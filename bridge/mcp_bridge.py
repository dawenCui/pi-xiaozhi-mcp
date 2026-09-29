"""Pi × 小智 WSS MCP Bridge.

连接小智云端 MCP endpoint（wss://api.xiaozhi.me/mcp/?token=...），
把本地 stdio MCP server（server.py）暴露给小智 LLM。

架构:
    小智云 LLM (DeepSeek/Qwen)
        ↕ WSS
    mcp_bridge.py (本文件)
        ↕ stdio
    server.py (MCPServer, pi_ask / pi_check_results / pi_status / pi_stop)
        ↕ subprocess
    pi --print

用法:
    # 从 mcp_config.json 启动所有 server
    python mcp_bridge.py

    # 指定单个 server 脚本
    python mcp_bridge.py path/to/server.py

环境变量:
    MCP_ENDPOINT  — 必须，从 xiaozhi.me 控制台获取的 WSS URL
    MCP_CONFIG    — 可选，mcp_config.json 路径（默认 ./mcp_config.json）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from logging.handlers import RotatingFileHandler

import websockets
from dotenv import load_dotenv

# .env 路径：仅在启动时通过 load_env() 显式加载，避免 import 副作用
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")


def load_env() -> None:
    """加载与本文件同目录的 .env（不受启动 cwd 影响）。"""
    load_dotenv(ENV_FILE)

# ── 日志 ──────────────────────────────────────────────────────────────
LOG_FILE = "/tmp/pi-xiaozhi-bridge.log"

os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_formatter)

_stderr_handler = logging.StreamHandler()
_stderr_handler.setFormatter(_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stderr_handler])
logger = logging.getLogger("pi-bridge")


def redact_token(uri: str) -> str:
    """隐藏 URL 中的 token，避免日志泄露凭据。"""
    base, sep, _ = uri.partition("?")
    return f"{base}?token=***" if sep else uri


# ── 重连设置 ──────────────────────────────────────────────────────────
INITIAL_BACKOFF = 1       # 首次重连等待秒数
MAX_BACKOFF = 600         # 最大等待秒数（10 分钟）


# ── 配置加载 ──────────────────────────────────────────────────────────

def load_config() -> dict:
    """从 $MCP_CONFIG 或 ./mcp_config.json 加载配置，失败返回空字典。"""
    path = os.environ.get("MCP_CONFIG") or os.path.join(os.getcwd(), "mcp_config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("配置 %s 顶层不是 JSON 对象，已忽略", path)
            return {}
        return data
    except Exception as e:
        logger.warning("加载配置 %s 失败: %s", path, e)
        return {}


def _build_child_env(entry: dict) -> dict:
    """构建子进程环境变量：继承当前进程，再叠加配置中的 env。"""
    child_env = os.environ.copy()
    for key, value in (entry.get("env") or {}).items():
        child_env[str(key)] = str(value)
    return child_env


def _build_proxy_command(entry: dict, target: str, typ: str) -> list[str]:
    """构建 mcp_proxy 子进程命令（用于 sse/http/streamablehttp 远端 server）。"""
    url = entry.get("url")
    if not url:
        raise RuntimeError(f"Server '{target}' (type {typ}) 缺少 'url'")

    cmd = [sys.executable, "-m", "mcp_proxy"]
    if typ in ("http", "streamablehttp"):
        cmd += ["--transport", "streamablehttp"]
    for key, value in (entry.get("headers") or {}).items():
        cmd += ["-H", key, str(value)]
    cmd.append(url)
    return cmd


def build_server_command(target: str) -> tuple[list[str], dict]:
    """根据 target 名称构建 server 子进程命令。

    优先从 mcp_config.json 的 mcpServers 里匹配；
    否则把 target 当作 Python 脚本路径（向后兼容）。
    """
    cfg = load_config()
    servers = cfg.get("mcpServers", {})

    # 向后兼容：target 当作脚本路径
    if target not in servers:
        if not os.path.exists(target):
            raise RuntimeError(
                f"'{target}' 既不是配置中的 server，也不是存在的脚本文件"
            )
        return [sys.executable, target], os.environ.copy()

    entry = servers[target] or {}
    if entry.get("disabled"):
        raise RuntimeError(f"Server '{target}' 已在配置中禁用")

    child_env = _build_child_env(entry)
    typ = (entry.get("type") or entry.get("transportType") or "stdio").lower()

    if typ == "stdio":
        command = entry.get("command")
        if not command:
            raise RuntimeError(f"Server '{target}' 缺少 'command' 字段")
        return [command, *(entry.get("args") or [])], child_env

    if typ in ("sse", "http", "streamablehttp"):
        return _build_proxy_command(entry, target, typ), child_env

    raise RuntimeError(f"不支持的 server 类型: {typ}")


# ── WSS 连接与 stdio 管道 ─────────────────────────────────────────────

async def connect_with_retry(uri: str, target: str) -> None:
    """带指数退避的无限重连。连接成功后复位退避，避免长期运行后重连等待被锁死在最大值。"""
    reconnect_attempt = 0
    backoff = INITIAL_BACKOFF

    def on_connected() -> None:
        nonlocal reconnect_attempt, backoff
        reconnect_attempt = 0
        backoff = INITIAL_BACKOFF

    while True:
        try:
            if reconnect_attempt > 0:
                logger.info(
                    "[%s] 等待 %ds 后第 %d 次重连...",
                    target, backoff, reconnect_attempt,
                )
                await asyncio.sleep(backoff)

            await connect_to_server(uri, target, on_connected=on_connected)

        except Exception as e:
            reconnect_attempt += 1
            logger.warning("[%s] 连接断开 (第 %d 次): %s", target, reconnect_attempt, e)
            backoff = min(backoff * 2, MAX_BACKOFF)


async def connect_to_server(
    uri: str, target: str, on_connected: Callable[[], None] | None = None
) -> None:
    """连接 WSS 并启动本地 MCP server 子进程，建立双向管道。

    on_connected: 连接建立成功后的回调，用于复位重连退避。
    """
    logger.info("[%s] 连接 WebSocket: %s", target, redact_token(uri))
    async with websockets.connect(uri) as websocket:
        logger.info("[%s] WebSocket 连接成功", target)
        if on_connected:
            on_connected()

        cmd, env = build_server_command(target)
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            text=True,
            env=env,
        )
        logger.info("[%s] 启动 MCP server: %s", target, " ".join(cmd))

        try:
            # 三个协程并发运行：WSS→stdin、stdout→WSS、stderr→日志
            await asyncio.gather(
                pipe_websocket_to_process(websocket, process, target),
                pipe_process_to_websocket(process, websocket, target),
                pipe_process_stderr_to_log(process, target),
            )
        finally:
            logger.info("[%s] 终止 MCP server 进程", target)
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            logger.info("[%s] MCP server 已终止", target)


async def pipe_websocket_to_process(websocket, process, target: str) -> None:
    """WSS 收到的消息 → 写入子进程 stdin（每行一条）。"""
    try:
        while True:
            message = await websocket.recv()
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            logger.debug("[%s] << %s...", target, message[:120])
            process.stdin.write(message + "\n")
            process.stdin.flush()
    except Exception as e:
        logger.error("[%s] WSS→stdin 管道错误: %s", target, e)
        raise
    finally:
        if not process.stdin.closed:
            process.stdin.close()


async def pipe_process_to_websocket(process, websocket, target: str) -> None:
    """子进程 stdout → 发送到 WSS。"""
    try:
        while True:
            data = await asyncio.to_thread(process.stdout.readline)
            if not data:
                logger.info("[%s] 子进程 stdout 结束", target)
                break
            logger.debug("[%s] >> %s...", target, data[:120])
            await websocket.send(data)
    except Exception as e:
        logger.error("[%s] stdout→WSS 管道错误: %s", target, e)
        raise


async def pipe_process_stderr_to_log(process, target: str) -> None:
    """子进程 stderr → 通过 logger 写入日志，统一由 RotatingFileHandler 管理。"""
    try:
        while True:
            data = await asyncio.to_thread(process.stderr.readline)
            if not data:
                logger.info("[%s] 子进程 stderr 结束", target)
                break
            for line in data.splitlines():
                if line:
                    logger.info("[%s][stderr] %s", target, line)
    except Exception as e:
        logger.error("[%s] stderr 管道错误: %s", target, e)
        raise


# ── 入口 ──────────────────────────────────────────────────────────────

def signal_handler(_sig, _frame) -> None:
    logger.info("收到中断信号，正在退出...")
    sys.exit(0)


def register_signal_handlers() -> None:
    """注册 SIGINT / SIGTERM，供优雅退出（launchd 停止服务时发 SIGTERM）。"""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


if __name__ == "__main__":
    register_signal_handlers()
    load_env()

    endpoint_url = os.environ.get("MCP_ENDPOINT")
    if not endpoint_url:
        logger.error("请设置 MCP_ENDPOINT 环境变量（从 xiaozhi.me 控制台获取）")
        sys.exit(1)

    target_arg = sys.argv[1] if len(sys.argv) >= 2 else None

    async def _main() -> None:
        if not target_arg:
            # 从 mcp_config.json 启动所有启用的 server
            cfg = load_config()
            servers_cfg = cfg.get("mcpServers") or {}
            enabled = [
                name for name, entry in servers_cfg.items()
                if not (entry or {}).get("disabled")
            ]
            skipped = [name for name in servers_cfg if name not in enabled]
            if skipped:
                logger.info("跳过已禁用的 server: %s", ", ".join(skipped))
            if not enabled:
                raise RuntimeError("mcp_config.json 中没有可用的 server")
            logger.info("启动 server: %s", ", ".join(enabled))
            tasks = [
                asyncio.create_task(connect_with_retry(endpoint_url, name))
                for name in enabled
            ]
            await asyncio.gather(*tasks)
        else:
            # 单个脚本模式
            if os.path.exists(target_arg):
                await connect_with_retry(endpoint_url, target_arg)
            else:
                logger.error(
                    "参数必须是本地 Python 脚本路径。"
                    "要启动配置中的所有 server，请不要传参数。"
                )
                sys.exit(1)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.error("运行错误: %s", e)
