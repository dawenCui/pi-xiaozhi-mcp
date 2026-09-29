"""Tests for bridge/server.py MCP server."""
import asyncio
from unittest.mock import patch

import pytest
import pytest_asyncio

# Need to import after fixtures are set up


@pytest.mark.asyncio
async def test_pi_ask_sync_success(mock_subprocess_exec, mock_pi_process, reset_server_state):
    """pi_ask returns Pi output when execution completes within timeout."""
    # Configure mock to return quickly (within sync timeout)
    mock_pi_process.communicate.return_value = (b"1+1 equals 2", b"")
    mock_pi_process.returncode = 0

    from bridge.server import pi_ask

    result = await pi_ask("1+1等于几")

    assert "1+1 equals 2" in result
    # Verify pi was called with --continue flag
    call_args = mock_subprocess_exec.call_args
    assert "--continue" in call_args[0]


@pytest.mark.asyncio
async def test_pi_ask_timeout_fallback(mock_subprocess_exec, mock_pi_process, reset_server_state):
    """pi_ask returns busy message when sync times out, background completes later."""
    import time

    # Configure mock to timeout on first call (sync), succeed on second (async)
    call_count = [0]

    async def slow_communicate(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # First call (sync) - simulate timeout by taking longer than PI_SYNC_TIMEOUT
            await asyncio.sleep(0.2)  # Longer than test timeout
            raise asyncio.TimeoutError()
        else:
            # Second call (async background) - succeeds
            return (b"background result", b"")

    mock_pi_process.communicate.side_effect = slow_communicate
    mock_pi_process.returncode = 0

    from bridge.server import pi_ask, PI_SYNC_TIMEOUT

    # Set very short timeout for testing
    with patch("bridge.server.PI_SYNC_TIMEOUT", 0.1):
        result = await pi_ask("long running task")

    # Should get busy message immediately
    assert "Pai 正在处理" in result

    # Wait for background task to complete
    await asyncio.sleep(0.3)

    # Check that result was saved
    from bridge.server import pi_check_results

    check_result = await pi_check_results()
    assert "background result" in check_result or "成功" in check_result


@pytest.mark.asyncio
async def test_pi_ask_rejected_while_background_running(mock_subprocess_exec, mock_pi_process, reset_server_state):
    """超时转入后台后 busy 标志仍保持，新指令应被拒绝（全程互斥）。"""
    from unittest.mock import AsyncMock

    import bridge.server as server
    from bridge.server import pi_ask

    call_count = [0]

    async def sync_timeout_then_bg_slow(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            await asyncio.sleep(0.2)   # 同步阶段超时
            raise asyncio.TimeoutError()
        await asyncio.sleep(0.2)       # 后台阶段慢一点，保持 busy
        return (b"done", b"")

    mock_pi_process.communicate = AsyncMock(side_effect=sync_timeout_then_bg_slow)
    mock_pi_process.returncode = 0

    with patch("bridge.server.PI_SYNC_TIMEOUT", 0.1):
        first = await pi_ask("first task")

    assert "Pai 正在处理" in first
    assert server._pi_busy is True

    second = await pi_ask("second task")
    assert "Pai 正忙" in second

    await asyncio.sleep(0.3)
    assert server._pi_busy is False


@pytest.mark.asyncio
async def test_pi_ask_busy_rejection(reset_server_state):
    """pi_ask returns a busy message when Pi is already running."""
    import bridge.server as server
    from bridge.server import pi_ask

    server._pi_busy = True
    try:
        result = await pi_ask("test task")
        assert "Pai 正忙" in result
    finally:
        server._pi_busy = False


@pytest.mark.asyncio
async def test_pi_check_results_fifo(reset_server_state):
    """pi_check_results returns results in FIFO order and consumes them."""
    from bridge.server import _complete_task, pi_check_results

    # Add multiple results
    _complete_task("task1", True, "result1")
    _complete_task("task2", True, "result2")
    _complete_task("task3", False, "error3")

    # Should return in order
    r1 = await pi_check_results()
    assert "task1" in r1 and "result1" in r1

    r2 = await pi_check_results()
    assert "task2" in r2 and "result2" in r2

    r3 = await pi_check_results()
    assert "task3" in r3 and "error3" in r3

    # Should be empty now
    r4 = await pi_check_results()
    assert "暂时没有完成的任务" in r4


@pytest.mark.asyncio
async def test_pi_check_results_empty(reset_server_state):
    """pi_check_results returns message when no results available."""
    from bridge.server import pi_check_results

    result = await pi_check_results()
    assert "暂时没有完成的任务" in result


@pytest.mark.asyncio
async def test_background_task_error_handling(reset_server_state):
    """Background task exceptions are caught and reported."""
    from bridge.server import _complete_task, pi_check_results

    # Simulate error completion
    _complete_task("failing task", False, "some error occurred")

    result = await pi_check_results()
    assert "失败" in result or "failing task" in result


@pytest.mark.asyncio
async def test_pi_command_includes_continue_flag(mock_subprocess_exec, mock_pi_process, reset_server_state):
    """Pi command includes --continue flag for session continuity."""
    mock_pi_process.communicate.return_value = (b"output", b"")
    mock_pi_process.returncode = 0

    from bridge.server import pi_ask

    await pi_ask("test")

    # Verify --continue is in the command
    call_args = mock_subprocess_exec.call_args
    cmd_args = call_args[0]  # Positional args
    assert "--continue" in cmd_args


@pytest.mark.asyncio
async def test_pi_status_reports_busy_state(mock_subprocess_exec, reset_server_state):
    """pi_status correctly reports when Pi is busy."""
    from unittest.mock import AsyncMock, MagicMock

    import bridge.server as server
    from bridge.server import pi_status

    # Mock version check
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"pi v1.0", b""))
    mock_proc.returncode = 0
    mock_subprocess_exec.return_value = mock_proc

    server._pi_busy = True
    try:
        result = await pi_status()
        assert "忙" in result
    finally:
        server._pi_busy = False


@pytest.mark.asyncio
async def test_pi_status_reports_idle_state(mock_subprocess_exec, mock_pi_process, reset_server_state):
    """pi_status correctly reports when Pi is idle."""
    from unittest.mock import AsyncMock, MagicMock

    from bridge.server import pi_status

    # Mock version check
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"pi v1.0", b""))
    mock_proc.returncode = 0
    mock_subprocess_exec.return_value = mock_proc

    result = await pi_status()
    assert "空闲" in result or "可用" in result


# ── 新增：错误路径 / 边界情况 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_pi_ask_file_not_found(mock_subprocess_exec, reset_server_state):
    """pi_ask reports a friendly error when the pi binary is missing."""
    from bridge.server import pi_ask

    mock_subprocess_exec.side_effect = FileNotFoundError("no pi")
    result = await pi_ask("hello")

    assert "找不到 pi 命令" in result


@pytest.mark.asyncio
async def test_pi_ask_nonzero_exit(mock_subprocess_exec, mock_pi_process, reset_server_state):
    """pi_ask reports the exit code and stderr on failure."""
    from bridge.server import pi_ask

    mock_pi_process.communicate.return_value = (b"", b"some error")
    mock_pi_process.returncode = 1

    result = await pi_ask("bad task")

    assert "执行失败" in result
    assert "退出码 1" in result
    assert "some error" in result


@pytest.mark.asyncio
async def test_pi_ask_empty_output(mock_subprocess_exec, mock_pi_process, reset_server_state):
    """pi_ask returns a placeholder when Pi produced no output."""
    from bridge.server import pi_ask

    mock_pi_process.communicate.return_value = (b"", b"")
    mock_pi_process.returncode = 0

    result = await pi_ask("empty task")

    assert "（Pi 没有返回内容）" in result


@pytest.mark.asyncio
async def test_pi_ask_truncates_long_output(mock_subprocess_exec, mock_pi_process, reset_server_state):
    """pi_ask truncates output longer than 500 chars."""
    from bridge.server import pi_ask

    mock_pi_process.communicate.return_value = (b"x" * 600, b"")
    mock_pi_process.returncode = 0

    result = await pi_ask("long task")

    assert "已截短" in result
    assert len(result) < 500


@pytest.mark.asyncio
async def test_pi_check_results_truncates_long(reset_server_state):
    """pi_check_results truncates results longer than 800 chars."""
    from bridge.server import _complete_task, pi_check_results

    _complete_task("long task", True, "y" * 1000)

    result = await pi_check_results()

    assert "已截短" in result


@pytest.mark.asyncio
async def test_pi_status_file_not_found(mock_subprocess_exec, reset_server_state):
    """pi_status reports unavailable when the binary is missing."""
    from bridge.server import pi_status

    mock_subprocess_exec.side_effect = FileNotFoundError("no pi")

    result = await pi_status()

    assert "不可用" in result


@pytest.mark.asyncio
async def test_pi_status_version_timeout(mock_subprocess_exec, reset_server_state):
    """pi_status reports a timeout when the version check times out."""
    import asyncio as aio
    from unittest.mock import AsyncMock, MagicMock

    from bridge.server import pi_status

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=aio.TimeoutError())
    mock_proc.returncode = 0
    mock_subprocess_exec.return_value = mock_proc

    result = await pi_status()

    assert "超时" in result


@pytest.mark.asyncio
async def test_pi_status_reports_queue_count(mock_subprocess_exec, reset_server_state):
    """pi_status reports the number of completed results awaiting pickup."""
    from unittest.mock import AsyncMock, MagicMock

    from bridge.server import _complete_task, pi_status

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"pi v1.0", b""))
    mock_proc.returncode = 0
    mock_subprocess_exec.return_value = mock_proc

    _complete_task("a", True, "x")
    _complete_task("b", True, "y")

    result = await pi_status()

    assert "2 个结果待查看" in result


@pytest.mark.asyncio
async def test_run_pi_sync_timeout_returns_proc(mock_subprocess_exec, mock_pi_process):
    """_run_pi_sync returns __TIMEOUT__ and the live process instead of killing it."""
    import asyncio as aio
    from unittest.mock import AsyncMock

    from bridge.server import _run_pi_sync

    async def slow(*args, **kwargs):
        await aio.sleep(1)
        return (b"", b"")

    mock_pi_process.communicate = AsyncMock(side_effect=slow)

    ok, output, proc = await _run_pi_sync("slow", 0.01, 1)

    assert ok is False
    assert output == "__TIMEOUT__"
    assert proc is mock_pi_process


@pytest.mark.asyncio
async def test_wait_for_proc_success(mock_pi_process):
    """_wait_for_proc returns output when the process finishes cleanly."""
    from unittest.mock import AsyncMock

    from bridge.server import _wait_for_proc

    mock_pi_process.communicate = AsyncMock(return_value=(b"done", b""))
    mock_pi_process.returncode = 0

    ok, text = await _wait_for_proc(mock_pi_process, 5, 1)

    assert ok is True
    assert "done" in text


@pytest.mark.asyncio
async def test_wait_for_proc_nonzero(mock_pi_process):
    """_wait_for_proc reports a nonzero exit code."""
    from unittest.mock import AsyncMock

    from bridge.server import _wait_for_proc

    mock_pi_process.communicate = AsyncMock(return_value=(b"", b"boom"))
    mock_pi_process.returncode = 2

    ok, text = await _wait_for_proc(mock_pi_process, 5, 1)

    assert ok is False
    assert "退出码 2" in text


@pytest.mark.asyncio
async def test_registered_tool_names():
    """The MCP server exposes exactly pi_ask / pi_check_results / pi_status."""
    from bridge.server import mcp

    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}

    assert names == {"pi_ask", "pi_check_results", "pi_status", "pi_stop"}


@pytest.mark.asyncio
async def test_tool_descriptions_not_empty():
    """每个 MCP 工具都必须带非空描述，供 LLM 理解调用方式。"""
    from bridge.server import mcp

    tools = await mcp.list_tools()

    for tool in tools:
        assert tool.description and tool.description.strip(), (
            f"tool '{tool.name}' 缺少描述"
        )


# ── pi_stop ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pi_stop_no_running_task(reset_server_state):
    """pi_stop 在没有任务时返回提示，不报错。"""
    from bridge.server import pi_stop

    result = await pi_stop()

    assert "没有正在执行的任务" in result


@pytest.mark.asyncio
async def test_pi_stop_kills_and_reports_termination(mock_subprocess_exec, mock_pi_process, reset_server_state):
    """pi_stop 杀死后台任务、释放锁，并产出「已终止」结果。"""
    from unittest.mock import AsyncMock

    import bridge.server as server
    from bridge.server import pi_ask, pi_check_results, pi_stop

    release = asyncio.Event()
    call_count = [0]

    async def sync_timeout_then_background(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            await asyncio.sleep(0.2)   # 同步阶段超时
            raise asyncio.TimeoutError()
        await release.wait()           # 后台阶段挂住，模拟长时间任务
        return (b"", b"")

    mock_pi_process.communicate = AsyncMock(side_effect=sync_timeout_then_background)
    mock_pi_process.returncode = None  # 模拟进程仍在运行

    with patch("bridge.server.PI_SYNC_TIMEOUT", 0.1):
        first = await pi_ask("long task")

    assert "Pai 正在处理" in first
    assert server._pi_busy is True
    assert server._current_proc is mock_pi_process

    # 终止：应 kill 进程并释放锁
    result = await pi_stop()
    assert "已终止" in result
    assert server._pi_busy is False
    assert server._current_proc is None
    mock_pi_process.kill.assert_called_once()

    # 模拟进程被杀死后收尾：returncode 变 -9，放行后台 waiter
    mock_pi_process.returncode = -9
    release.set()
    await asyncio.sleep(0.05)

    check = await pi_check_results()
    assert "任务已被用户终止" in check


# ── 代际防护 / 后台任务引用 / 同步不入队 / pi_status 健壮性 ──────────

def test_reset_runtime_state_ignores_stale_generation(reset_server_state):
    """过期代际的复位请求必须被忽略，避免覆盖新任务状态。"""
    import bridge.server as server

    server._pi_busy = True
    server._current_proc = object()
    server._current_task = "new task"
    server._generation = 2

    # 旧代际复位：忽略
    server._reset_runtime_state(1)
    assert server._pi_busy is True
    assert server._current_proc is not None

    # 当前代际复位：生效
    server._reset_runtime_state(2)
    assert server._pi_busy is False
    assert server._current_proc is None


@pytest.mark.asyncio
async def test_background_task_reference_is_kept(mock_subprocess_exec, mock_pi_process, reset_server_state):
    """超时转后台时保存 task 引用，完成后自动移除，避免被 GC。"""
    from unittest.mock import AsyncMock

    import bridge.server as server
    from bridge.server import pi_ask

    call_count = [0]

    async def timeout_then_done(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            await asyncio.sleep(0.05)   # 同步阶段超时
            raise asyncio.TimeoutError()
        return (b"done", b"")           # 后台阶段完成

    mock_pi_process.communicate = AsyncMock(side_effect=timeout_then_done)
    mock_pi_process.returncode = 0

    with patch("bridge.server.PI_SYNC_TIMEOUT", 0.01):
        await pi_ask("task")

    assert len(server._background_tasks) == 1

    await asyncio.sleep(0.1)
    assert len(server._background_tasks) == 0
    assert server._pi_busy is False


@pytest.mark.asyncio
async def test_stale_background_waiter_does_not_clobber_new_task(
    mock_subprocess_exec, mock_pi_process, reset_server_state
):
    """已停止任务的过期后台收尾不得覆盖新任务状态（代际防护）。"""
    from unittest.mock import AsyncMock, MagicMock

    import bridge.server as server
    from bridge.server import pi_ask, pi_stop

    old_release = asyncio.Event()
    old_calls = [0]

    async def old_communicate(*args, **kwargs):
        old_calls[0] += 1
        if old_calls[0] == 1:
            await asyncio.sleep(0.05)   # 同步阶段超时
            raise asyncio.TimeoutError()
        await old_release.wait()        # 旧任务后台阶段挂住
        return (b"", b"")

    mock_pi_process.communicate = AsyncMock(side_effect=old_communicate)
    mock_pi_process.returncode = None

    with patch("bridge.server.PI_SYNC_TIMEOUT", 0.01):
        first = await pi_ask("old task")

    assert "Pai 正在处理" in first
    assert server._current_proc is mock_pi_process

    await pi_stop()
    assert server._pi_busy is False

    # 新任务也超时转后台：busy 重新置位，_current_proc 指向新进程
    new_proc = MagicMock()
    new_proc.returncode = None
    new_release = asyncio.Event()
    new_calls = [0]

    async def new_communicate(*args, **kwargs):
        new_calls[0] += 1
        if new_calls[0] == 1:
            await asyncio.sleep(0.05)
            raise asyncio.TimeoutError()
        await new_release.wait()
        return (b"", b"")

    new_proc.communicate = AsyncMock(side_effect=new_communicate)
    mock_subprocess_exec.return_value = new_proc

    with patch("bridge.server.PI_SYNC_TIMEOUT", 0.01):
        second = await pi_ask("new task")

    assert "Pai 正在处理" in second
    assert server._pi_busy is True
    assert server._current_proc is new_proc

    # 放行旧任务后台收尾：不应覆盖新任务状态
    mock_pi_process.returncode = -9
    old_release.set()
    await asyncio.sleep(0.05)

    assert server._pi_busy is True
    assert server._current_proc is new_proc

    # 清理：让新任务正常结束
    new_proc.returncode = 0
    new_release.set()
    await asyncio.sleep(0.05)
    assert server._pi_busy is False


@pytest.mark.asyncio
async def test_pi_ask_sync_result_not_enqueued(mock_subprocess_exec, mock_pi_process, reset_server_state):
    """同步成功结果直接返回，不应进入后台结果队列。"""
    from bridge.server import pi_ask, pi_check_results

    mock_pi_process.communicate.return_value = (b"2", b"")
    mock_pi_process.returncode = 0

    result = await pi_ask("1+1")
    assert "2" in result

    check = await pi_check_results()
    assert "暂时没有完成的任务" in check


@pytest.mark.asyncio
async def test_pi_status_nonzero_exit(mock_subprocess_exec, reset_server_state):
    """版本检查非零退出时返回异常提示。"""
    from unittest.mock import AsyncMock, MagicMock

    from bridge.server import pi_status

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"boom"))
    mock_proc.returncode = 1
    mock_subprocess_exec.return_value = mock_proc

    result = await pi_status()

    assert "退出码 1" in result


@pytest.mark.asyncio
async def test_pi_status_empty_version(mock_subprocess_exec, reset_server_state):
    """版本输出为空时显示未知，而不是空串。"""
    from unittest.mock import AsyncMock, MagicMock

    from bridge.server import pi_status

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0
    mock_subprocess_exec.return_value = mock_proc

    result = await pi_status()

    assert "版本: 未知" in result


# ── 参数解析健壮性 / 日志隐私 / 二次 communicate ────────────────────

def test_parse_extra_args_supports_quoting():
    """PI_EXTRA_ARGS 用 shlex 解析，支持带引号/含空格的参数。"""
    from bridge.server import _parse_extra_args

    assert _parse_extra_args("") == []
    assert _parse_extra_args("--model") == ["--model"]
    assert _parse_extra_args('--model "deepseek v4 pro" --flag') == [
        "--model",
        "deepseek v4 pro",
        "--flag",
    ]


def test_env_int_clamps_and_falls_back(monkeypatch):
    """PI_SYNC_TIMEOUT：非法值回退默认，且不小于下限。"""
    from bridge.server import _env_int

    monkeypatch.setenv("PI_SYNC_TIMEOUT", "0")
    assert _env_int("PI_SYNC_TIMEOUT", 4, 1) == 1

    monkeypatch.setenv("PI_SYNC_TIMEOUT", "abc")
    assert _env_int("PI_SYNC_TIMEOUT", 4, 1) == 4

    monkeypatch.setenv("PI_SYNC_TIMEOUT", "10")
    assert _env_int("PI_SYNC_TIMEOUT", 4, 1) == 10


@pytest.mark.asyncio
async def test_run_pi_sync_truncates_logged_prompt(mock_subprocess_exec, mock_pi_process, caplog):
    """长 prompt 只写预览到 INFO，完整内容降到 DEBUG。"""
    import logging

    from bridge.server import _run_pi_sync

    mock_pi_process.communicate.return_value = (b"", b"")
    mock_pi_process.returncode = 0

    long_prompt = "长" * 500
    with caplog.at_level(logging.INFO):
        await _run_pi_sync(long_prompt, 5, 1)

    messages = [r.getMessage() for r in caplog.records]
    joined = "\n".join(messages)
    assert long_prompt not in joined
    assert ("长" * 200) in joined


@pytest.mark.asyncio
async def test_communicate_retry_after_timeout():
    """锁定 asyncio 行为：同步超时后二次 communicate 仍能取回输出。"""
    import asyncio as aio

    proc = await aio.create_subprocess_exec(
        "sh", "-c", "sleep 0.2; echo done",
        stdout=aio.subprocess.PIPE,
        stderr=aio.subprocess.PIPE,
    )
    try:
        try:
            await aio.wait_for(proc.communicate(), timeout=0.05)
        except aio.TimeoutError:
            pass
        stdout, _ = await aio.wait_for(proc.communicate(), timeout=5)
        assert b"done" in stdout
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
