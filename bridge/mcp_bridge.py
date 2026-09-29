"""Pi × 小智 WSS MCP Bridge.

连接小智云端 MCP endpoint（wss://api.xiaozhi.me/mcp/?token=...），
把本地 stdio MCP server（server.py）暴露给小智 LLM。

架构:
    小智云 LLM (DeepSeek/Qwen)
        ↕ WSS
    mcp_bridge.py (本文件)
        ↕ stdio
    server.py (FastMCP, pi_task / pi_quick / ...)
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

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from logging.handlers import RotatingFileHandler

import websockets
from dotenv import load_dotenv

# 自动加载 .env
load_dotenv()

# ── 日志 ──────────────────────────────────────────────────────────────
LOG_FILE = "/tmp/pi-xiaozhi-bridge.log"

os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)

formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(formatter)

stderr_handler = logging.StreamHandler()
stderr_handler.setFormatter(formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, stderr_handler])
logger = logging.getLogger("pi-bridge")

# ── 重连设置 ──────────────────────────────────────────────────────────
INITIAL_BACKOFF = 1       # 首次重连等待秒数
MAX_BACKOFF = 600         # 最大等待秒数（10 分钟）


# ── 配置加载 ──────────────────────────────────────────────────────────

def load_config() -> dict:
    """从 $MCP_CONFIG 或 ./mcp_config.json 加载配置。"""
    path = os.environ.get("MCP_CONFIG") or os.path.join(os.getcwd(), "mcp_config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("加载配置 %s 失败: %s", path, e)
        return {}


def build_server_command(target: str) -> tuple[list[str], dict]:
    """根据 target 名称构建 server 子进程命令。

    优先从 mcp_config.json 的 mcpServers 里匹配，
    否则把 target 当作 Python 脚本路径（向后兼容）。
    """
    cfg = load_config()
    servers = cfg.get("mcpServers", {}) if isinstance(cfg, dict) else {}

    if target in servers:
        entry = servers[target] or {}
        if entry.get("disabled"):
            raise RuntimeError(f"Server '{target}' 已在配置中禁用")

        # 子进程环境变量
        child_env = os.environ.copy()
        for k, v in (entry.get("env") or {}).items():
            child_env[str(k)] = str(v)

        typ = (entry.get("type") or entry.get("transportType") or "stdio").lower()

        if typ == "stdio":
            command = entry.get("command")
            args = entry.get("args") or []
            if not command:
                raise RuntimeError(f"Server '{target}' 缺少 'command' 字段")
            return [command, *args], child_env

        if typ in ("sse", "http", "streamablehttp"):
            url = entry.get("url")
            if not url:
                raise RuntimeError(f"Server '{target}' (type {typ}) 缺少 'url'")
            cmd = [sys.executable, "-m", "mcp_proxy"]
            if typ in ("http", "streamablehttp"):
                cmd += ["--transport", "streamablehttp"]
            headers = entry.get("headers") or {}
            for hk, hv in headers.items():
                cmd += ["-H", hk, str(hv)]
            cmd.append(url)
            return cmd, child_env

        raise RuntimeError(f"不支持的 server 类型: {typ}")

    # 向后兼容：target 当作脚本路径
    script_path = target
    if not os.path.exists(script_path):
        raise RuntimeError(
            f"'{target}' 既不是配置中的 server，也不是存在的脚本文件"
        )
    return [sys.executable, script_path], os.environ.copy()


# ── WSS 连接与 stdio 管道 ─────────────────────────────────────────────

async def connect_with_retry(uri: str, target: str):
    """带指数退避的无限重连。"""
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

            await connect_to_server(uri, target)

        except Exception as e:
            reconnect_attempt += 1
            logger.warning("[%s] 连接断开 (第 %d 次): %s", target, reconnect_attempt, e)
            log_bridge_event("disconnect", f"[{target}] 连接断开: {e}")
            backoff = min(backoff * 2, MAX_BACKOFF)


async def connect_to_server(uri: str, target: str):
    """连接 WSS 并启动本地 MCP server 子进程，双向管道。"""
    logger.info("[%s] 连接 WebSocket: %s", target, uri.split("?")[0] + "?token=***")
    async with websockets.connect(uri) as websocket:
        logger.info("[%s] WebSocket 连接成功", target)

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


async def pipe_websocket_to_process(websocket, process, target: str):
    """WSS 收到的消息 → 写入子进程 stdin。"""
    try:
        while True:
            message = await websocket.recv()
            logger.debug("[%s] << %s...", target, message[:120])
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            process.stdin.write(message + "\n")
            process.stdin.flush()
    except Exception as e:
        logger.error("[%s] WSS→stdin 管道错误: %s", target, e)
        raise
    finally:
        if not process.stdin.closed:
            process.stdin.close()


async def pipe_process_to_websocket(process, websocket, target: str):
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


async def pipe_process_stderr_to_log(process, target: str):
    """子进程 stderr → 滚动日志文件。"""
    try:
        while True:
            data = await asyncio.to_thread(process.stderr.readline)
            if not data:
                logger.info("[%s] 子进程 stderr 结束", target)
                break
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(data)
    except Exception as e:
        logger.error("[%s] stderr 管道错误: %s", target, e)
        raise


# ── 入口 ──────────────────────────────────────────────────────────────

def signal_handler(sig, frame):
    logger.info("收到中断信号，正在退出...")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)

    endpoint_url = os.environ.get("MCP_ENDPOINT")
    if not endpoint_url:
        logger.error("请设置 MCP_ENDPOINT 环境变量（从 xiaozhi.me 控制台获取）")
        sys.exit(1)

    target_arg = sys.argv[1] if len(sys.argv) >= 2 else None

    async def _main():
        if not target_arg:
            # 从 mcp_config.json 启动所有 server
            cfg = load_config()
            servers_cfg = cfg.get("mcpServers") or {}
            all_names = list(servers_cfg.keys())
            enabled = [
                name for name, entry in servers_cfg.items()
                if not (entry or {}).get("disabled")
            ]
            skipped = [name for name in all_names if name not in enabled]
            if skipped:
                logger.info("跳过已禁用的 server: %s", ", ".join(skipped))
            if not enabled:
                raise RuntimeError("mcp_config.json 中没有可用的 server")
            logger.info("启动 server: %s", ", ".join(enabled))
            tasks = [
                asyncio.create_task(connect_with_retry(endpoint_url, t))
                for t in enabled
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
