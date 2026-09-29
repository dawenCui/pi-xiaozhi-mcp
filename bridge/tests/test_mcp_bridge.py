"""Tests for bridge/mcp_bridge.py WSS ↔ stdio bridge."""
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── load_config ──────────────────────────────────────────────────────

def test_load_config_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "nope.json"))

    from bridge import mcp_bridge

    assert mcp_bridge.load_config() == {}


def test_load_config_invalid_json(tmp_path, monkeypatch):
    p = tmp_path / "mcp_config.json"
    p.write_text("{not valid json")
    monkeypatch.setenv("MCP_CONFIG", str(p))

    from bridge import mcp_bridge

    assert mcp_bridge.load_config() == {}


def test_load_config_valid(tmp_path, monkeypatch):
    p = tmp_path / "mcp_config.json"
    p.write_text(json.dumps({"mcpServers": {"x": {"command": "pi"}}}))
    monkeypatch.setenv("MCP_CONFIG", str(p))

    from bridge import mcp_bridge

    assert mcp_bridge.load_config()["mcpServers"]["x"]["command"] == "pi"


def test_load_config_json_not_object(tmp_path, monkeypatch):
    """顶层不是 JSON 对象时返回空字典，避免后续 .get 崩溃。"""
    p = tmp_path / "mcp_config.json"
    p.write_text(json.dumps(["a", "b"]))
    monkeypatch.setenv("MCP_CONFIG", str(p))

    from bridge import mcp_bridge

    assert mcp_bridge.load_config() == {}


# ── build_server_command ─────────────────────────────────────────────

def test_build_server_command_stdio(tmp_path, monkeypatch):
    cfg = {
        "mcpServers": {
            "myserver": {
                "command": "python",
                "args": ["server.py"],
                "env": {"FOO": "bar"},
            }
        }
    }
    p = tmp_path / "mcp_config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_CONFIG", str(p))

    from bridge import mcp_bridge

    cmd, env = mcp_bridge.build_server_command("myserver")

    assert cmd == ["python", "server.py"]
    assert env["FOO"] == "bar"


def test_build_server_command_disabled_raises(tmp_path, monkeypatch):
    cfg = {"mcpServers": {"x": {"command": "python", "disabled": True}}}
    p = tmp_path / "mcp_config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_CONFIG", str(p))

    from bridge import mcp_bridge

    with pytest.raises(RuntimeError, match="禁用"):
        mcp_bridge.build_server_command("x")


def test_build_server_command_missing_command_raises(tmp_path, monkeypatch):
    cfg = {"mcpServers": {"x": {"args": ["server.py"]}}}
    p = tmp_path / "mcp_config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_CONFIG", str(p))

    from bridge import mcp_bridge

    with pytest.raises(RuntimeError, match="command"):
        mcp_bridge.build_server_command("x")


def test_build_server_command_http_with_headers(tmp_path, monkeypatch):
    cfg = {
        "mcpServers": {
            "x": {
                "type": "http",
                "url": "http://localhost:8080/mcp",
                "headers": {"Authorization": "Bearer abc"},
            }
        }
    }
    p = tmp_path / "mcp_config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_CONFIG", str(p))

    from bridge import mcp_bridge

    cmd, _ = mcp_bridge.build_server_command("x")

    assert cmd[0] == sys.executable
    assert "-m" in cmd and "mcp_proxy" in cmd
    assert "--transport" in cmd and "streamablehttp" in cmd
    assert "http://localhost:8080/mcp" in cmd
    # header pair
    assert "-H" in cmd and "Authorization" in cmd and "Bearer abc" in cmd


def test_build_server_command_sse(tmp_path, monkeypatch):
    cfg = {"mcpServers": {"x": {"type": "sse", "url": "http://localhost:8080/sse"}}}
    p = tmp_path / "mcp_config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_CONFIG", str(p))

    from bridge import mcp_bridge

    cmd, _ = mcp_bridge.build_server_command("x")

    assert cmd[-1] == "http://localhost:8080/sse"
    assert "--transport" not in cmd  # sse does not force streamablehttp


def test_build_server_command_fallback_script(tmp_path, monkeypatch):
    script = tmp_path / "myserver.py"
    script.write_text("print('hi')")
    monkeypatch.delenv("MCP_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # no mcp_config.json in cwd

    from bridge import mcp_bridge

    cmd, _ = mcp_bridge.build_server_command(str(script))

    assert cmd == [sys.executable, str(script)]


def test_build_server_command_unknown_target_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "missing.json"))

    from bridge import mcp_bridge

    with pytest.raises(RuntimeError):
        mcp_bridge.build_server_command("no_such_server")


def test_build_server_command_unsupported_type_raises(tmp_path, monkeypatch):
    cfg = {"mcpServers": {"x": {"type": "custom", "command": "pi"}}}
    p = tmp_path / "mcp_config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_CONFIG", str(p))

    from bridge import mcp_bridge

    with pytest.raises(RuntimeError, match="不支持"):
        mcp_bridge.build_server_command("x")


# ── 管道函数 ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pipe_process_to_websocket_sends_lines():
    from bridge import mcp_bridge

    websocket = MagicMock()
    websocket.send = AsyncMock()
    process = MagicMock()
    process.stdout.readline = MagicMock(side_effect=["hello\n", "world\n", ""])

    await mcp_bridge.pipe_process_to_websocket(process, websocket, "t")

    assert websocket.send.await_count == 2
    websocket.send.assert_any_await("hello\n")
    websocket.send.assert_any_await("world\n")


@pytest.mark.asyncio
async def test_pipe_websocket_to_process_writes_and_flushes():
    from bridge import mcp_bridge

    websocket = MagicMock()
    websocket.recv = AsyncMock(side_effect=["msg1", b"msg2", Exception("closed")])
    process = MagicMock()
    process.stdin = MagicMock()
    process.stdin.closed = False

    with pytest.raises(Exception, match="closed"):
        await mcp_bridge.pipe_websocket_to_process(websocket, process, "t")

    assert process.stdin.write.call_count == 2
    assert process.stdin.flush.call_count == 2
    process.stdin.close.assert_called_once()


@pytest.mark.asyncio
async def test_pipe_process_stderr_to_log():
    from bridge import mcp_bridge

    process = MagicMock()
    process.stderr.readline = MagicMock(side_effect=["err1\n", "err2\n", ""])

    with patch.object(mcp_bridge.logger, "info") as info_mock:
        await mcp_bridge.pipe_process_stderr_to_log(process, "t")

    messages = []
    for call in info_mock.call_args_list:
        args = call.args
        messages.append(args[0] % args[1:] if len(args) > 1 else args[0])
    joined = " ".join(messages)
    assert "err1" in joined
    assert "err2" in joined


# ── 连接生命周期 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_to_server_pipes_and_cleans_up():
    from bridge import mcp_bridge

    fake_ws = MagicMock()
    ws_cm = MagicMock()
    ws_cm.__aenter__ = AsyncMock(return_value=fake_ws)
    ws_cm.__aexit__ = AsyncMock(return_value=False)

    fake_process = MagicMock()

    with patch.object(
        mcp_bridge, "build_server_command", return_value=(["python", "server.py"], {})
    ) as build_mock, patch("websockets.connect", return_value=ws_cm), patch.object(
        mcp_bridge.subprocess, "Popen", return_value=fake_process
    ) as popen_mock, patch.object(
        mcp_bridge, "pipe_websocket_to_process", new=AsyncMock()
    ) as p1, patch.object(
        mcp_bridge, "pipe_process_to_websocket", new=AsyncMock()
    ) as p2, patch.object(
        mcp_bridge, "pipe_process_stderr_to_log", new=AsyncMock()
    ) as p3:

        await mcp_bridge.connect_to_server("ws://example/mcp?token=secret", "myserver")

    build_mock.assert_called_once_with("myserver")
    popen_mock.assert_called_once()
    p1.assert_awaited_once_with(fake_ws, fake_process, "myserver")
    p2.assert_awaited_once_with(fake_process, fake_ws, "myserver")
    p3.assert_awaited_once_with(fake_process, "myserver")
    fake_process.terminate.assert_called_once()
    fake_process.wait.assert_called_once_with(timeout=5)


@pytest.mark.asyncio
async def test_connect_with_retry_backoff_and_retry():
    from bridge import mcp_bridge

    connect_mock = AsyncMock(side_effect=[Exception("down"), KeyboardInterrupt()])

    with patch.object(mcp_bridge, "connect_to_server", connect_mock), patch.object(
        mcp_bridge.asyncio, "sleep", new=AsyncMock()
    ) as sleep_mock:

        with pytest.raises(KeyboardInterrupt):
            await mcp_bridge.connect_with_retry("ws://example", "t")

    assert connect_mock.await_count == 2
    # backoff doubles after the first failure: 1 -> 2
    sleep_mock.assert_awaited_once_with(mcp_bridge.INITIAL_BACKOFF * 2)


@pytest.mark.asyncio
async def test_connect_with_retry_resets_backoff_after_connect():
    """连接成功后复位退避，避免多次断线后重连等待被锁死在最大值。"""
    from bridge import mcp_bridge

    calls = []

    async def fake_connect(uri, target, on_connected=None):
        calls.append(1)
        if len(calls) <= 2:
            if on_connected:
                on_connected()          # 模拟连接建立成功
            raise Exception("drop")     # 随后断开
        raise KeyboardInterrupt()

    with patch.object(mcp_bridge, "connect_to_server", new=fake_connect), patch.object(
        mcp_bridge.asyncio, "sleep", new=AsyncMock()
    ) as sleep_mock:
        with pytest.raises(KeyboardInterrupt):
            await mcp_bridge.connect_with_retry("ws://example", "t")

    # 每次成功连接后 backoff 都复位为 1，再因断开翻倍为 2，
    # 不会累积到 4/8/... 因此两次 sleep 都应为 INITIAL_BACKOFF * 2。
    assert sleep_mock.await_count == 2
    sleep_mock.assert_awaited_with(mcp_bridge.INITIAL_BACKOFF * 2)


# ── 信号处理 ─────────────────────────────────────────────────────────

def test_register_signal_handlers():
    """注册 SIGINT 与 SIGTERM（launchd 停止服务时发 SIGTERM）。"""
    import signal as signal_mod

    from bridge import mcp_bridge

    with patch.object(mcp_bridge.signal, "signal") as signal_mock:
        mcp_bridge.register_signal_handlers()

    registered = [call.args[0] for call in signal_mock.call_args_list]
    assert signal_mod.SIGINT in registered
    assert signal_mod.SIGTERM in registered


# ── 环境变量加载 ───────────────────────────────────────────────────

def test_load_env_reads_env_file(tmp_path, monkeypatch):
    """load_env() 按需加载与脚本同目录的 .env，而不是在 import 时副作用。"""
    import os

    from bridge import mcp_bridge

    env_file = tmp_path / ".env"
    env_file.write_text("LOAD_ENV_TEST_VAR=hello\n")
    monkeypatch.setattr(mcp_bridge, "ENV_FILE", str(env_file))
    monkeypatch.delenv("LOAD_ENV_TEST_VAR", raising=False)

    mcp_bridge.load_env()

    assert os.environ["LOAD_ENV_TEST_VAR"] == "hello"
