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
# 让类型注解按字符串处理，不急于求值，方便引用尚未定义的名称。
from __future__ import annotations

# asyncio：事件循环、并发跑三个管道协程、sleep 退避等。
import asyncio
# json：解析 mcp_config.json。
import json
# logging：写日志（文件 + stderr）。
import logging
# os：读环境变量、路径拼接、复制环境变量给子进程。
import os
# signal：注册 SIGINT/SIGTERM，优雅退出。
import signal
# subprocess：启动 server.py 子进程。
import subprocess
# sys：拿到当前解释器路径、退出码。
import sys
# Callable：给 on_connected 回调做类型标注。
from collections.abc import Callable
# RotatingFileHandler：日志按大小滚动，避免单个文件无限增大。
from logging.handlers import RotatingFileHandler

# websockets：连接 xiaozhi.me 的 WSS MCP 端点。
import websockets
# load_dotenv：把 .env 里的变量注入环境。
from dotenv import load_dotenv

# .env 路径：固定用本文件同目录下的 .env，不受启动时 cwd 影响。
# 只记录路径，不在这里加载——真正的加载放到 load_env()，避免 import 副作用。
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")


def load_env() -> None:
    """加载与本文件同目录的 .env（不受启动 cwd 影响）。

    为什么延迟到显式调用：如果放在模块顶层 import 时执行，任何
    `import mcp_bridge` 的代码（含测试）都会偷偷改全局环境变量，
    造成难以排查的副作用。
    """
    # 把 ENV_FILE 里的 KEY=VALUE 写入 os.environ（已存在的变量默认不覆盖）。
    load_dotenv(ENV_FILE)

# ── 日志 ──────────────────────────────────────────────────────────────
# 日志文件路径：固定放 /tmp，方便 launchd 和手动排查。
LOG_FILE = "/tmp/pi-xiaozhi-bridge.log"

# 确保 /tmp 存在（dirname("/tmp/...") 是 "/tmp"）。
os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)

# 统一的日志格式：时间 + 级别 + 内容。
_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

# 文件 handler：10MB 一个文件，最多保留 5 个历史文件。
_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_formatter)

# stderr handler：前台运行时也能直接看到日志。
_stderr_handler = logging.StreamHandler()
_stderr_handler.setFormatter(_formatter)

# 同时挂上文件 + stderr 两个 handler。
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stderr_handler])
logger = logging.getLogger("pi-bridge")


def redact_token(uri: str) -> str:
    """隐藏 URL 中的 token，避免日志泄露凭据。

    为什么：日志会写到 /tmp 文件，若把完整 WSS URL（含 token）打进去，
    等于把访问令牌明文落盘，有安全风险。
    """
    # 把 URL 从 "?" 处拆成 base + 查询串；sep 为空表示没有查询串。
    base, sep, _ = uri.partition("?")
    # 有查询串就统一替换成 token=***；没有就原样返回。
    return f"{base}?token=***" if sep else uri


# ── 重连设置 ──────────────────────────────────────────────────────────
INITIAL_BACKOFF = 1       # 首次重连等待秒数
MAX_BACKOFF = 600         # 最大等待秒数（10 分钟）


# ── 配置加载 ──────────────────────────────────────────────────────────

def load_config() -> dict:
    """从 $MCP_CONFIG 或 ./mcp_config.json 加载配置，失败返回空字典。

    为什么失败要返回 {} 而不是抛异常：配置只是可选辅助，缺了不应让进程崩溃；
    让上层统一处理“没有可用 server”的情况。
    """
    # 优先用环境变量 MCP_CONFIG 指定的路径，否则用当前目录下的 mcp_config.json。
    path = os.environ.get("MCP_CONFIG") or os.path.join(os.getcwd(), "mcp_config.json")
    # 文件不存在直接返回空字典。
    if not os.path.exists(path):
        return {}
    try:
        # 读文件并解析 JSON。
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 顶层必须是对象；若是数组/字符串等，视为无效配置，返回 {}。
        # 这样上层就能放心调用 cfg.get(...) 而不会 AttributeError。
        if not isinstance(data, dict):
            logger.warning("配置 %s 顶层不是 JSON 对象，已忽略", path)
            return {}
        return data
    except Exception as e:
        # JSON 解析失败等异常：记日志并返回空字典。
        logger.warning("加载配置 %s 失败: %s", path, e)
        return {}


def _build_child_env(entry: dict) -> dict:
    """构建子进程环境变量：继承当前进程，再叠加配置中的 env。

    为什么先 copy 再覆盖：子进程需要继承 MCP_ENDPOINT、PI_* 等当前进程变量，
    同时允许 mcp_config.json 里的 env 覆盖或新增。
    """
    # 复制当前进程全部环境变量作为基础。
    child_env = os.environ.copy()
    # 把配置里的 env 键值逐条写进去（值统一转成字符串）。
    for key, value in (entry.get("env") or {}).items():
        child_env[str(key)] = str(value)
    return child_env


def _build_proxy_command(entry: dict, target: str, typ: str) -> list[str]:
    """构建 mcp_proxy 子进程命令（用于 sse/http/streamablehttp 远端 server）。

    为什么走 mcp_proxy：这些是远端 HTTP/SSE 服务，不是本地脚本，需要代理转发。
    """
    # url 是必须字段。
    url = entry.get("url")
    if not url:
        raise RuntimeError(f"Server '{target}' (type {typ}) 缺少 'url'")

    # 基础命令：用当前解释器跑 `python -m mcp_proxy`。
    cmd = [sys.executable, "-m", "mcp_proxy"]
    # http / streamablehttp 要显式指定 streamablehttp 传输。
    if typ in ("http", "streamablehttp"):
        cmd += ["--transport", "streamablehttp"]
    # 把配置里的 headers 转成 `-H key value` 参数对。
    for key, value in (entry.get("headers") or {}).items():
        cmd += ["-H", key, str(value)]
    # 最后拼上目标 URL。
    cmd.append(url)
    return cmd


def build_server_command(target: str) -> tuple[list[str], dict]:
    """根据 target 名称构建 server 子进程命令。

    优先从 mcp_config.json 的 mcpServers 里匹配；
    否则把 target 当作 Python 脚本路径（向后兼容）。
    """
    # 读取配置；load_config 保证返回 dict，这里直接 .get 即可。
    cfg = load_config()
    servers = cfg.get("mcpServers", {})

    # 向后兼容：target 不在配置里时，把它当作本地脚本路径。
    if target not in servers:
        # 脚本不存在就给明确报错。
        if not os.path.exists(target):
            raise RuntimeError(
                f"'{target}' 既不是配置中的 server，也不是存在的脚本文件"
            )
        # 用当前解释器跑这个脚本，环境变量直接继承当前进程。
        return [sys.executable, target], os.environ.copy()

    # 取到对应条目；None 时用空 dict 兜底。
    entry = servers[target] or {}
    # disabled 的 server 直接拒绝启动。
    if entry.get("disabled"):
        raise RuntimeError(f"Server '{target}' 已在配置中禁用")

    # 先把子进程环境准备好。
    child_env = _build_child_env(entry)
    # type / transportType 二选一，默认 stdio。
    typ = (entry.get("type") or entry.get("transportType") or "stdio").lower()

    if typ == "stdio":
        # 本地 stdio 服务：需要 command 字段。
        command = entry.get("command")
        if not command:
            raise RuntimeError(f"Server '{target}' 缺少 'command' 字段")
        # 命令 = [command, ...args]。
        return [command, *(entry.get("args") or [])], child_env

    if typ in ("sse", "http", "streamablehttp"):
        # 远端服务走 mcp_proxy 代理。
        return _build_proxy_command(entry, target, typ), child_env

    # 其它类型不支持，报错。
    raise RuntimeError(f"不支持的 server 类型: {typ}")


# ── WSS 连接与 stdio 管道 ─────────────────────────────────────────────

async def connect_with_retry(uri: str, target: str) -> None:
    """带指数退避的无限重连。连接成功后复位退避，避免长期运行后重连等待被锁死在最大值。"""
    # 第几次重连（用于日志）。
    reconnect_attempt = 0
    # 当前退避等待秒数。
    backoff = INITIAL_BACKOFF

    def on_connected() -> None:
        # 连接成功后复位：attempt 归零、backoff 回到初始值。
        # 为什么用闭包 nonlocal：要修改外层 connect_with_retry 的局部变量。
        nonlocal reconnect_attempt, backoff
        reconnect_attempt = 0
        backoff = INITIAL_BACKOFF

    # 无限重连循环。
    while True:
        try:
            # 第 0 次不等待；之后每次先等 backoff 秒再连。
            if reconnect_attempt > 0:
                logger.info(
                    "[%s] 等待 %ds 后第 %d 次重连...",
                    target, backoff, reconnect_attempt,
                )
                await asyncio.sleep(backoff)

            # 真正建立连接 + 双向管道；正常会一直跑到断开为止。
            await connect_to_server(uri, target, on_connected=on_connected)

        except Exception as e:
            # 断开/失败：重连次数 +1，并记录。
            reconnect_attempt += 1
            logger.warning("[%s] 连接断开 (第 %d 次): %s", target, reconnect_attempt, e)
            # 指数退避翻倍，封顶 MAX_BACKOFF。
            backoff = min(backoff * 2, MAX_BACKOFF)


async def connect_to_server(
    uri: str, target: str, on_connected: Callable[[], None] | None = None
) -> None:
    """连接 WSS 并启动本地 MCP server 子进程，建立双向管道。

    on_connected: 连接建立成功后的回调，用于复位重连退避。
    """
    # 打日志时隐藏 token。
    logger.info("[%s] 连接 WebSocket: %s", target, redact_token(uri))
    # async with 保证退出时自动关闭 WebSocket。
    async with websockets.connect(uri) as websocket:
        logger.info("[%s] WebSocket 连接成功", target)
        # 连上了就通知上层复位退避（有回调时才调）。
        if on_connected:
            on_connected()

        # 根据 target 构建本地 server 子进程命令和环境。
        cmd, env = build_server_command(target)
        # 启动子进程；stdin/stdout/stderr 都走管道，文本模式按 UTF-8。
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
            # 三个协程并发运行：WSS→stdin、stdout→WSS、stderr→日志。
            # gather 任一协程异常，就会取消另外两个并向上抛。
            await asyncio.gather(
                pipe_websocket_to_process(websocket, process, target),
                pipe_process_to_websocket(process, websocket, target),
                pipe_process_stderr_to_log(process, target),
            )
        finally:
            # 无论连接怎么结束，都要清理子进程。
            logger.info("[%s] 终止 MCP server 进程", target)
            try:
                # 先礼貌地 terminate，等 5 秒。
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # 5 秒还不退就强杀。
                process.kill()
            logger.info("[%s] MCP server 已终止", target)


async def pipe_websocket_to_process(websocket, process, target: str) -> None:
    """WSS 收到的消息 → 写入子进程 stdin（每行一条）。

    为什么每行一条：MCP stdio 协议是 newline-delimited JSON，每条消息一行。
    """
    try:
        while True:
            # 阻塞等待一条 WSS 消息。
            message = await websocket.recv()
            # websockets 可能返回 bytes，统一转成 str。
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            logger.debug("[%s] << %s...", target, message[:120])
            # 写一行 + 换行，然后 flush 立刻送入子进程。
            process.stdin.write(message + "\n")
            process.stdin.flush()
    except Exception as e:
        # 管道错误：记录并向上抛，让 gather 结束整个连接。
        logger.error("[%s] WSS→stdin 管道错误: %s", target, e)
        raise
    finally:
        # 结束后关闭 stdin，通知子进程没有更多输入了。
        if not process.stdin.closed:
            process.stdin.close()


async def pipe_process_to_websocket(process, websocket, target: str) -> None:
    """子进程 stdout → 发送到 WSS。"""
    try:
        while True:
            # 用线程读一行，避免阻塞事件循环（Popen 的 readline 是同步阻塞的）。
            data = await asyncio.to_thread(process.stdout.readline)
            # readline 返回空字符串表示 stdout 已到 EOF，结束循环。
            if not data:
                logger.info("[%s] 子进程 stdout 结束", target)
                break
            logger.debug("[%s] >> %s...", target, data[:120])
            # 把这一行原样发回 WSS。
            await websocket.send(data)
    except Exception as e:
        # 发送失败：记录并抛出。
        logger.error("[%s] stdout→WSS 管道错误: %s", target, e)
        raise


async def pipe_process_stderr_to_log(process, target: str) -> None:
    """子进程 stderr → 通过 logger 写入日志，统一由 RotatingFileHandler 管理。

    为什么走 logger 而不是自己 open 文件：自己 open 的句柄不会随日志
    轮转更新，且会与 RotatingFileHandler 并发写同一文件导致交叠。
    """
    try:
        while True:
            # 线程读一行 stderr。
            data = await asyncio.to_thread(process.stderr.readline)
            if not data:
                logger.info("[%s] 子进程 stderr 结束", target)
                break
            # 一次可能含多行，逐行写日志；空行跳过。
            for line in data.splitlines():
                if line:
                    logger.info("[%s][stderr] %s", target, line)
    except Exception as e:
        logger.error("[%s] stderr 管道错误: %s", target, e)
        raise


# ── 入口 ──────────────────────────────────────────────────────────────

def signal_handler(_sig, _frame) -> None:
    """收到中断信号时的处理：记录后退出。"""
    logger.info("收到中断信号，正在退出...")
    # 直接退出进程。
    sys.exit(0)


def register_signal_handlers() -> None:
    """注册 SIGINT / SIGTERM，供优雅退出（launchd 停止服务时发 SIGTERM）。

    为什么还要注册 SIGTERM：用户 Ctrl+C 发 SIGINT，但 launchd 停止服务时
    发的是 SIGTERM，只注册 SIGINT 的话 launchd 场景走不到优雅退出。
    """
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


# 仅当直接运行 `python mcp_bridge.py` 时执行。
if __name__ == "__main__":
    # 先注册信号处理，再加载 .env。
    register_signal_handlers()
    load_env()

    # MCP_ENDPOINT 是必须项，没配就直接报错退出。
    endpoint_url = os.environ.get("MCP_ENDPOINT")
    if not endpoint_url:
        logger.error("请设置 MCP_ENDPOINT 环境变量（从 xiaozhi.me 控制台获取）")
        sys.exit(1)

    # 命令行可带一个参数：本地 server 脚本路径；不带则从配置启动所有 server。
    target_arg = sys.argv[1] if len(sys.argv) >= 2 else None

    async def _main() -> None:
        # 分支一：没传参数，从 mcp_config.json 启动所有启用的 server。
        if not target_arg:
            cfg = load_config()
            servers_cfg = cfg.get("mcpServers") or {}
            # enabled：未被 disabled 的 server 名列表。
            enabled = [
                name for name, entry in servers_cfg.items()
                if not (entry or {}).get("disabled")
            ]
            # skipped：被禁用的 server 名（用于日志提示）。
            skipped = [name for name in servers_cfg if name not in enabled]
            if skipped:
                logger.info("跳过已禁用的 server: %s", ", ".join(skipped))
            # 一个可用的都没有就直接报错。
            if not enabled:
                raise RuntimeError("mcp_config.json 中没有可用的 server")
            logger.info("启动 server: %s", ", ".join(enabled))
            # 每个 server 一个独立的“重连”任务，并发跑。
            tasks = [
                asyncio.create_task(connect_with_retry(endpoint_url, name))
                for name in enabled
            ]
            # 等所有任务（通常永不返回，直到进程被信号中断）。
            await asyncio.gather(*tasks)
        else:
            # 分支二：传了脚本路径，只启动这一个。
            if os.path.exists(target_arg):
                await connect_with_retry(endpoint_url, target_arg)
            else:
                logger.error(
                    "参数必须是本地 Python 脚本路径。"
                    "要启动配置中的所有 server，请不要传参数。"
                )
                sys.exit(1)

    try:
        # 进入事件循环跑 _main。
        asyncio.run(_main())
    except KeyboardInterrupt:
        # Ctrl+C。
        logger.info("用户中断")
    except Exception as e:
        # 其它未捕获异常，记日志后退出。
        logger.error("运行错误: %s", e)
