"""Pi Agent MCP Server.

把本地 pi agent 包装成 MCP 工具，暴露给小智云 LLM（DeepSeek/Qwen）。
当用户语音指令被 LLM 判定需要 pi 处理时，LLM 通过 WSS MCP endpoint 调用本工具，
本工具 spawn `pi --print` 执行任务，把结果回流给 LLM 让 TTS 朗读。

工具清单:
  pi_ask(task)        — 同步优先，超时自动转异步；统一入口
  pi_check_results()  — FIFO 取下一个已完成结果
  pi_status()         — Pi 可用性 + 任务队列状态
  pi_stop()           — 终止当前任务并释放锁，让 Pi 回到空闲

环境变量:
  PI_BIN         — pi 命令路径（默认 pi）
  PI_WORKDIR     — pi 工作目录（默认 ~）
  PI_EXTRA_ARGS  — pi 额外参数（如 --model deepseek/deepseek-v4-pro）
  PI_SYNC_TIMEOUT — 同步等待秒数（默认 4）
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import shlex
import sys

from mcp.server.mcpserver import MCPServer

# ── 配置 ─────────────────────────────────────────────────────────────
PI_BIN = os.environ.get("PI_BIN", "pi")
PI_WORKDIR = os.environ.get("PI_WORKDIR", os.path.expanduser("~"))


def _parse_extra_args(raw: str) -> list[str]:
    """解析 PI_EXTRA_ARGS：用 shlex 支持引号与转义。"""
    return shlex.split(raw) if raw else []


def _env_int(name: str, default: int, minimum: int) -> int:
    """读取整数环境变量，非法值回退默认，并保证不小于 minimum。"""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


PI_EXTRA_ARGS = _parse_extra_args(os.environ.get("PI_EXTRA_ARGS", ""))
PI_SYNC_TIMEOUT = _env_int("PI_SYNC_TIMEOUT", 4, 1)

# 同步超时转后台后，后台继续等待的时长上限
ASYNC_TIMEOUT_SECS = 300

# 输出截断阈值：避免 TTS 朗读过长内容
MAX_ASK_OUTPUT_CHARS = 500    # pi_ask 直接返回时的上限
ASK_OUTPUT_TRUNCATED = 400    # 超限后保留的长度
MAX_RESULT_CHARS = 800        # pi_check_results 返回时的上限
RESULT_TRUNCATED = 600        # 超限后保留的长度
VERSION_CHECK_TIMEOUT_SECS = 5  # pi_status 探测版本的超时
MAX_LOG_PROMPT_CHARS = 200    # pi_ask 日志中 prompt 的预览长度

# ── 日志 ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("pi-mcp")

# ── MCP server 与运行状态 ────────────────────────────────────────────
mcp = MCPServer("pi-xiaozhi-mcp")

# Pi 是有状态的（session + 历史），同一时间只能跑一个。
# `_pi_busy` 是唯一互斥标志，覆盖「同步等待 + 后台等待」整个进程生命周期；
# 检查与置位之间没有 await，因此在单线程事件循环内是原子的。
_pi_busy = False

# 当前正在执行的进程及其任务描述，供 pi_stop 杀死；无任务时为 None / ""
_current_proc: asyncio.subprocess.Process | None = None
_current_task = ""

# 任务代际：每启动一个新任务自增。用于防止「已停止/过期」任务的后台收尾
# 覆盖掉新任务的状态（busy 标志、当前进程引用等）。
_generation = 0

# 被 pi_stop 请求终止的任务代际；0 表示没有终止请求。
# 用代际而非全局 bool，避免旧任务读到新任务的终止状态。
_stop_requested_gen = 0

# 保存后台任务引用，避免 create_task 返回的 task 被 GC 导致收尾丢失。
_background_tasks: set[asyncio.Task] = set()

# FIFO 结果队列：仅保存「后台」任务结果，供 pi_check_results 按 FIFO 取用。
_completed: collections.deque[tuple[str, bool, str]] = collections.deque()


def _is_stopped(gen: int) -> bool:
    """判断指定代际的任务是否已被用户请求终止。"""
    return _stop_requested_gen == gen


def _complete_task(desc: str, ok: bool, output: str) -> None:
    """把后台任务结果写入队列，供 pi_check_results 按 FIFO 取用。"""
    status = "OK" if ok else "FAIL"
    _completed.append((desc, ok, output))
    logger.info("task done [%s]: %s", status, desc[:40])


def _reset_runtime_state(gen: int | None = None) -> None:
    """释放互斥标志，并清空当前进程引用。

    gen 用于防竞态：仅当传入代际仍是当前代际时才允许复位，
    避免过期任务的后台收尾覆盖新任务状态。
    """
    global _pi_busy, _current_proc, _current_task
    if gen is not None and gen != _generation:
        return
    _pi_busy = False
    _current_proc = None
    _current_task = ""


def _kill_process(proc: asyncio.subprocess.Process) -> None:
    """尽力 SIGKILL 进程，忽略已退出等异常。"""
    if proc.returncode is not None:
        return  # 进程已结束
    try:
        proc.kill()
    except ProcessLookupError:
        pass  # 进程已不存在


def _decode_result(
    proc: asyncio.subprocess.Process,
    stdout: bytes,
    stderr: bytes,
    gen: int,
) -> tuple[bool, str]:
    """把已结束进程的输出转成 (成功?, 文本)。"""
    if proc.returncode != 0:
        if _is_stopped(gen):
            return False, "任务已被用户终止"
        err = (stderr or b"").decode(errors="replace")[-500:]
        return False, f"pi 退出码 {proc.returncode}: {err}"

    return True, (stdout or b"").decode(errors="replace").strip()


async def _run_pi_sync(
    prompt: str, timeout: int, gen: int
) -> tuple[bool, str, asyncio.subprocess.Process | None]:
    """同步运行 `pi --print`，超时**不杀进程**，把存活进程交还调用方。

    Returns:
        (成功?, 输出文本或 "__TIMEOUT__", 超时时的存活进程或 None)
    """
    global _current_proc, _current_task

    # 日志不包含 prompt（用户语音内容），避免隐私内容写进日志
    cmd = [PI_BIN, "--print", "--continue", *PI_EXTRA_ARGS, prompt]
    logger.info("pi run: %s", " ".join(cmd[:-1]))
    preview = prompt[:MAX_LOG_PROMPT_CHARS]
    if len(prompt) > MAX_LOG_PROMPT_CHARS:
        preview += "..."
    logger.info("pi input: %s", preview)
    logger.debug("pi input (full):\n%s", prompt)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PI_WORKDIR,
            env={**os.environ},
        )
    except FileNotFoundError:
        return False, f"找不到 pi 命令: {PI_BIN}，请检查 PI_BIN 环境变量", None
    except Exception as e:
        logger.exception("pi run failed")
        return False, f"调用失败: {type(e).__name__}: {e}", None

    # 记录当前进程，供 pi_stop 终止
    _current_proc = proc
    _current_task = prompt

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # 超时不杀进程，让它在后台继续跑，稍后由 _wait_for_proc 收尾。
        # 此处取消了一次 communicate()，后台会再次调用 proc.communicate()；
        # 现代 asyncio 支持「cancel 后重新 communicate」，
        # 由 test_communicate_retry_after_timeout 锁定该行为。
        logger.warning("pi timeout (>%ds), continuing in background", timeout)
        return False, "__TIMEOUT__", proc
    except Exception as e:
        logger.exception("pi run failed")
        return False, f"调用失败: {type(e).__name__}: {e}", None

    ok, text = _decode_result(proc, stdout, stderr, gen)
    if ok:
        logger.info("pi output:\n%s", text)
    else:
        logger.error("pi task failed: %s", text)
    return ok, text, None


async def _wait_for_proc(
    proc: asyncio.subprocess.Process, timeout: int, gen: int
) -> tuple[bool, str]:
    """等待一个已在后台运行的进程结束，并收集其输出。"""
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # 后台也超时：主动杀死，避免留下孤儿进程并长期占用互斥锁
        logger.warning("background pi wait timeout (>%ds), killing", timeout)
        _kill_process(proc)
        return False, f"后台任务超时（>{timeout}s），已终止"
    except Exception as e:
        logger.exception("background pi wait failed")
        return False, f"后台执行失败: {type(e).__name__}: {e}"

    return _decode_result(proc, stdout, stderr, gen)


def _spawn_background_wait(
    proc: asyncio.subprocess.Process, task: str, gen: int
) -> None:
    """为超时转后台的进程派发协程，结束后写结果并释放 busy 标志。"""
    async def _bg() -> None:
        try:
            ok, output = await _wait_for_proc(proc, ASYNC_TIMEOUT_SECS, gen)
            _complete_task(task, ok, output)
        finally:
            _reset_runtime_state(gen)

    bg = asyncio.create_task(_bg())
    _background_tasks.add(bg)
    bg.add_done_callback(_background_tasks.discard)


# ── 工具定义 ──────────────────────────────────────────────────────────

@mcp.tool(description=(
    f"调用本地 Pi Agent 执行任务（统一入口）。\n"
    f"先同步等待 {PI_SYNC_TIMEOUT} 秒，出结果直接返回；超时自动转后台，"
    "并提示用户稍后查询结果。\n\n"
    "Args:\n"
    "    task: 给 Pi 的中文指令，越具体越好。例如：\n"
    '        "帮我查一下今天的天气"\n'
    '        "写一篇关于 AI 的 200 字短文"\n'
    '        "看看当前目录有什么文件"'
))
async def pi_ask(task: str) -> str:
    """执行任务的统一入口：同步优先，超时自动转后台。"""
    global _pi_busy, _generation
    # 检查与置位之间没有 await，保证单线程事件循环内原子
    if _pi_busy:
        return "Pai 正忙，请稍后再试"

    _pi_busy = True
    _generation += 1
    my_gen = _generation
    try:
        ok, output, timed_out_proc = await _run_pi_sync(task, PI_SYNC_TIMEOUT, my_gen)
    except Exception:
        _reset_runtime_state(my_gen)  # 意外异常也要释放，避免卡死后续调用
        raise

    if output == "__TIMEOUT__":
        # 进程仍在运行：busy 标志转交后台任务持有，直到进程真正结束
        _spawn_background_wait(timed_out_proc, task, my_gen)
        desc = task[:30]
        return f"Pai 正在处理「{desc}」，稍后问我结果"

    _reset_runtime_state(my_gen)
    # 同步结果直接返回给 LLM，不入队；pi_check_results 只服务后台任务

    if not ok:
        return f"执行失败: {output}"
    if len(output) > MAX_ASK_OUTPUT_CHARS:
        return output[:ASK_OUTPUT_TRUNCATED] + "\n（结果较长已截短）"
    return output or "（Pi 没有返回内容）"


@mcp.tool()
async def pi_check_results() -> str:
    """检查 Pi 后台任务是否完成，FIFO 取下一个结果，取完即消。"""
    if not _completed:
        return "暂时没有完成的任务"

    desc, ok, output = _completed.popleft()
    status = "成功" if ok else "失败"
    text = f"[{status}] {desc}\n\n{output or '（无输出）'}"

    if len(text) > MAX_RESULT_CHARS:
        text = text[:RESULT_TRUNCATED] + "\n\n（结果较长，已截短）"
    return text


@mcp.tool()
async def pi_status() -> str:
    """查询 Pi 可用性、工作目录，以及待取结果数量。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            PI_BIN, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PI_WORKDIR,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=VERSION_CHECK_TIMEOUT_SECS
        )
        if proc.returncode != 0:
            return f"Pai 状态异常：版本检查退出码 {proc.returncode}"
        version = stdout.decode(errors="replace").strip() or "未知"
    except FileNotFoundError:
        return f"Pai 不可用：找不到命令 {PI_BIN}"
    except asyncio.TimeoutError:
        return "Pai 状态检查超时"
    except Exception as e:
        return f"Pai 状态异常: {e}"

    busy = "忙" if _pi_busy else "空闲"
    return (
        f"Pai 可用，版本: {version}，工作目录: {PI_WORKDIR}\n"
        f"状态: {busy}, {len(_completed)} 个结果待查看"
    )


@mcp.tool()
async def pi_stop() -> str:
    """终止当前正在执行（含后台运行）的 pi 任务并释放锁，让 Pai 回到空闲。

    用于任务卡住或耗时过长、用户要求中止的场景；重复调用安全。
    """
    global _stop_requested_gen

    if _current_proc is None:
        return "Pai 当前没有正在执行的任务"

    desc = _current_task[:30]

    _stop_requested_gen = _generation
    _kill_process(_current_proc)
    _reset_runtime_state(_generation)

    return f"已终止「{desc}」，Pai 现在空闲"


# ── 入口 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("pi-xiaozhi-mcp server starting")
    logger.info("  PI_BIN=%s", PI_BIN)
    logger.info("  PI_WORKDIR=%s", PI_WORKDIR)
    logger.info("  PI_EXTRA_ARGS=%s", PI_EXTRA_ARGS)
    logger.info("  PI_SYNC_TIMEOUT=%ds", PI_SYNC_TIMEOUT)
    mcp.run()
