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
# 让所有类型注解（如 list[str]、X | None）都按“字符串”处理，不急着求值；
# 这样可以在类/函数定义之前引用还未定义的名称，也能用新语法。
from __future__ import annotations

# asyncio：提供事件循环、子进程、超时控制、Task 等异步能力。
import asyncio
# collections.deque：FIFO 队列，存后台任务结果。
import collections
# logging：写日志到 stderr，便于 launchd 收集 / 排障。
import logging
# os：读环境变量、展开路径、复制环境变量给子进程。
import os
# shlex：按 shell 规则拆分 PI_EXTRA_ARGS，支持引号和转义。
import shlex
# sys：日志输出到 stderr。
import sys

# 从 mcp 库导入 MCPServer，用来注册 pi_* 这几个工具。
from mcp.server.mcpserver import MCPServer

# ── 配置 ─────────────────────────────────────────────────────────────
# pi 可执行文件路径：优先取环境变量 PI_BIN，没配就用 PATH 里的 "pi"。
PI_BIN = os.environ.get("PI_BIN", "pi")

# pi 的工作目录：默认是用户主目录，让 pi 在固定上下文里运行。
PI_WORKDIR = os.environ.get("PI_WORKDIR", os.path.expanduser("~"))


def _parse_extra_args(raw: str) -> list[str]:
    """解析 PI_EXTRA_ARGS：用 shlex 支持引号与转义。

    为什么用 shlex.split 而不是 str.split：
    str.split 会把 "--model \"deepseek v4\"" 拆成 4 段，破坏带空格的参数；
    shlex.split 按 shell 规则拆，能正确处理引号。
    """
    # 空字符串直接返回空列表；非空则用 shlex 拆成参数列表。
    return shlex.split(raw) if raw else []


def _env_int(name: str, default: int, minimum: int) -> int:
    """读取整数环境变量，非法值回退默认，并保证不小于 minimum。

    为什么需要它：PI_SYNC_TIMEOUT 必须是正整数；用户可能配成 0、负数或
    非数字，直接 int() 会在 import 时崩溃。这里统一兜底，避免启动失败。
    """
    # 读原始字符串；没配就返回默认值。
    raw = os.environ.get(name, "")
    if not raw:
        return default
    # 尝试转 int；失败（比如配了 "abc"）就回退默认值。
    try:
        value = int(raw)
    except ValueError:
        return default
    # 保证不低于下限（例如超时时间至少 1 秒）。
    return max(minimum, value)


# 把 PI_EXTRA_ARGS 解析成列表，供后面拼子进程命令时展开。
PI_EXTRA_ARGS = _parse_extra_args(os.environ.get("PI_EXTRA_ARGS", ""))

# 同步等待秒数：4 秒内出结果直接返回，超时转后台。
# 下限 1 秒，避免配成 0/负数导致行为异常。
PI_SYNC_TIMEOUT = _env_int("PI_SYNC_TIMEOUT", 4, 1)

# 同步超时转后台后，后台最多再等这么久；再超时就杀掉，防止孤儿进程长期占锁。
ASYNC_TIMEOUT_SECS = 300

# 输出截断阈值：避免 TTS 朗读过长内容。
MAX_ASK_OUTPUT_CHARS = 500    # pi_ask 直接返回时的上限（超过就截断）
ASK_OUTPUT_TRUNCATED = 400    # 超限后保留的字符数
MAX_RESULT_CHARS = 800        # pi_check_results 返回文本的上限
RESULT_TRUNCATED = 600        # 超限后保留的字符数
VERSION_CHECK_TIMEOUT_SECS = 5  # pi_status 探测 --version 的超时
MAX_LOG_PROMPT_CHARS = 200    # 日志里 prompt 预览的长度（完整内容只进 DEBUG）

# ── 日志 ─────────────────────────────────────────────────────────────
# 配置日志：INFO 级别，写到 stderr（mcp_bridge 会把子进程 stderr 收进日志文件）。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
# 用统一的名字 "pi-mcp" 取 logger，方便按模块过滤日志。
logger = logging.getLogger("pi-mcp")

# ── MCP server 与运行状态 ────────────────────────────────────────────
# 创建 MCP server 实例，名字只是标识。
mcp = MCPServer("pi-xiaozhi-mcp")

# Pi 是有状态的（session + 历史），同一时间只能跑一个。
# `_pi_busy` 是唯一互斥标志，覆盖「同步等待 + 后台等待」整个进程生命周期；
# 检查与置位之间没有 await，因此在单线程事件循环内是原子的。
_pi_busy = False

# 当前正在执行的子进程及其任务描述，供 pi_stop 杀死；无任务时为 None / ""。
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
    """判断指定代际的任务是否已被用户请求终止。

    为什么按代际判断：pi_stop 只终止“当时正在跑的那一代”，
    新任务有不同的 gen，不能误判成被终止。
    """
    # 若记录的“被终止代际”等于当前代际，说明这个任务被用户要求中止了。
    return _stop_requested_gen == gen


def _complete_task(desc: str, ok: bool, output: str) -> None:
    """把后台任务结果写入队列，供 pi_check_results 按 FIFO 取用。

    为什么只写后台结果：同步结果已经直接念给用户了，再入队会导致
    用户之后问“结果呢”时又听一遍旧答案。
    """
    # 拼一个人类可读的状态词，用于日志。
    status = "OK" if ok else "FAIL"
    # 结果三元组 (描述, 是否成功, 输出) 追加到 FIFO 队列尾部。
    _completed.append((desc, ok, output))
    # 日志只截取前 40 个字符，避免超长描述刷屏。
    logger.info("task done [%s]: %s", status, desc[:40])


def _reset_runtime_state(gen: int | None = None) -> None:
    """释放互斥标志，并清空当前进程引用。

    gen 用于防竞态：仅当传入代际仍是当前代际时才允许复位，
    避免过期任务的后台收尾覆盖新任务状态。
    """
    # 声明要修改的是模块级全局变量（否则会被当成局部变量赋值）。
    global _pi_busy, _current_proc, _current_task
    # 防竞态核心：如果这次复位属于“上一代”任务，而已经有新任务启动了，
    # 就什么都不做，避免把新任务的状态清掉。
    if gen is not None and gen != _generation:
        return
    # 释放互斥锁，让后续 pi_ask 可以再次进入。
    _pi_busy = False
    # 清掉当前进程引用，表示现在没有在跑的任务。
    _current_proc = None
    # 清掉当前任务描述。
    _current_task = ""


def _kill_process(proc: asyncio.subprocess.Process) -> None:
    """尽力 SIGKILL 进程，忽略已退出等异常。

    为什么用 kill（SIGKILL）而不是 terminate（SIGTERM）：
    这里用于用户主动中止或后台超时，需要确保进程一定被干掉，不能给它讨价还价的机会。
    """
    # 如果 returncode 已经不是 None，说明进程已经结束，不需要再 kill。
    if proc.returncode is not None:
        return  # 进程已结束
    try:
        # 直接发 SIGKILL。
        proc.kill()
    except ProcessLookupError:
        # 进程刚好在 kill 前已经不存在，忽略即可。
        pass  # 进程已不存在


def _decode_result(
    proc: asyncio.subprocess.Process,
    stdout: bytes,
    stderr: bytes,
    gen: int,
) -> tuple[bool, str]:
    """把已结束进程的输出转成 (成功?, 文本)。

    为什么抽这个函数：同步路径和后台路径都要做同一套“看退出码 + 解码输出”，
    抽出来避免两处写重、以后改错一边。
    """
    # 退出码非 0 表示失败。
    if proc.returncode != 0:
        # 如果是被用户 pi_stop 中止的，返回友好提示而不是冷冰冰的退出码。
        if _is_stopped(gen):
            return False, "任务已被用户终止"
        # 取 stderr 最后 500 字符作为错误摘要（太长的报错只留尾部，通常最有信息量）。
        err = (stderr or b"").decode(errors="replace")[-500:]
        return False, f"pi 退出码 {proc.returncode}: {err}"

    # 成功：解码 stdout，strip 去掉首尾空行/空白。
    return True, (stdout or b"").decode(errors="replace").strip()


async def _run_pi_sync(
    prompt: str, timeout: int, gen: int
) -> tuple[bool, str, asyncio.subprocess.Process | None]:
    """同步运行 `pi --print`，超时**不杀进程**，把存活进程交还调用方。

    Returns:
        (成功?, 输出文本或 "__TIMEOUT__", 超时时的存活进程或 None)

    为什么超时不杀：pi 的任务（比如写文章）只是慢，杀掉就白跑了；
    这里把它交给后台继续跑，只把“正在处理”的回执先返回给 LLM。
    """
    # 声明要修改两个模块级全局变量。
    global _current_proc, _current_task

    # 拼完整命令：pi --print --continue [额外参数] [prompt]。
    # --continue 让 pi 延续会话历史，保持上下文。
    # 日志里 cmd[:-1] 去掉最后的 prompt，因为 prompt 是用户语音内容，属隐私。
    cmd = [PI_BIN, "--print", "--continue", *PI_EXTRA_ARGS, prompt]
    logger.info("pi run: %s", " ".join(cmd[:-1]))
    # 只打前 200 字符预览，避免长指令刷日志；超长补 "..."。
    preview = prompt[:MAX_LOG_PROMPT_CHARS]
    if len(prompt) > MAX_LOG_PROMPT_CHARS:
        preview += "..."
    logger.info("pi input: %s", preview)
    # 完整 prompt 只放 DEBUG 级别，平时看不到。
    logger.debug("pi input (full):\n%s", prompt)

    try:
        # 启动子进程；stdout/stderr 都用 PIPE 捕获，之后读回。
        # cwd 指定工作目录，env 继承当前环境。
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PI_WORKDIR,
            env={**os.environ},
        )
    except FileNotFoundError:
        # pi 命令不存在（PATH 里没有或 PI_BIN 配错了），给友好提示。
        return False, f"找不到 pi 命令: {PI_BIN}，请检查 PI_BIN 环境变量", None
    except Exception as e:
        # 其它启动异常：记日志并返回错误文本。
        logger.exception("pi run failed")
        return False, f"调用失败: {type(e).__name__}: {e}", None

    # 子进程启动成功后，记录它，供 pi_stop 随时能找到并杀掉。
    _current_proc = proc
    _current_task = prompt

    try:
        # 等待进程结束并读回 stdout/stderr；最多等 timeout 秒。
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # 超时不杀进程，让它在后台继续跑，稍后由 _wait_for_proc 收尾。
        # 此处取消了一次 communicate()，后台会再次调用 proc.communicate()；
        # 现代 asyncio 支持「cancel 后重新 communicate」，
        # 由 test_communicate_retry_after_timeout 锁定该行为。
        logger.warning("pi timeout (>%ds), continuing in background", timeout)
        # 返回哨兵字符串 "__TIMEOUT__" + 还活着的进程，交给 pi_ask 转后台。
        return False, "__TIMEOUT__", proc
    except Exception as e:
        # 等待过程中其它异常。
        logger.exception("pi run failed")
        return False, f"调用失败: {type(e).__name__}: {e}", None

    # 进程已经结束：统一用 _decode_result 解释退出码和输出。
    ok, text = _decode_result(proc, stdout, stderr, gen)
    if ok:
        # 成功就记录完整输出（同步任务输出通常不长，方便排查）。
        logger.info("pi output:\n%s", text)
    else:
        # 失败记录错误文本。
        logger.error("pi task failed: %s", text)
    # 同步路径最后统一返回 (ok, text, None)：没有需要交还的存活进程。
    return ok, text, None


async def _wait_for_proc(
    proc: asyncio.subprocess.Process, timeout: int, gen: int
) -> tuple[bool, str]:
    """等待一个已在后台运行的进程结束，并收集其输出。

    这是“同步阶段超时后”的收尾函数：进程还在跑，这里继续等到它结束。
    """
    try:
        # 再次 communicate() 等待进程结束；后台最多再等 timeout 秒。
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # 后台也超时：主动杀掉，避免留下孤儿进程并长期占用互斥锁。
        logger.warning("background pi wait timeout (>%ds), killing", timeout)
        _kill_process(proc)
        return False, f"后台任务超时（>{timeout}s），已终止"
    except Exception as e:
        # 其它异常。
        logger.exception("background pi wait failed")
        return False, f"后台执行失败: {type(e).__name__}: {e}"

    # 结束后的解释逻辑和同步路径一致，直接复用。
    return _decode_result(proc, stdout, stderr, gen)


def _spawn_background_wait(
    proc: asyncio.subprocess.Process, task: str, gen: int
) -> None:
    """为超时转后台的进程派发协程，结束后写结果并释放 busy 标志。

    为什么用协程而不是线程：这里是纯 asyncio 等待，用线程反而要处理同步原语；
    协程能直接 await 子进程，最自然。
    """
    async def _bg() -> None:
        # 后台收尾：等进程结束 -> 把结果写进 FIFO 队列。
        try:
            ok, output = await _wait_for_proc(proc, ASYNC_TIMEOUT_SECS, gen)
            _complete_task(task, ok, output)
        finally:
            # 无论成功/失败/异常，都要释放 busy 标志，否则后续调用会被卡死。
            _reset_runtime_state(gen)

    # 把 _bg 协程包成一个 Task 投进事件循环，立即返回（不阻塞 pi_ask）。
    bg = asyncio.create_task(_bg())
    # 关键：保存 task 引用，防止被垃圾回收导致收尾丢失。
    _background_tasks.add(bg)
    # 任务结束（无论结果如何）后，从集合里移除自己，避免内存泄漏。
    bg.add_done_callback(_background_tasks.discard)


# ── 工具定义 ──────────────────────────────────────────────────────────

# 注册 pi_ask 工具；description 是给 LLM 看的“使用说明书”，越具体 LLM 越会用。
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
    # 声明要修改两个模块级全局变量。
    global _pi_busy, _generation
    # 检查与置位之间没有 await，保证单线程事件循环内原子。
    # 为什么原子很关键：如果两个请求同时进来，都先 await 再检查，
    # 就可能都通过检查、同时启动两个 pi，导致有状态会话互相污染。
    if _pi_busy:
        return "Pai 正忙，请稍后再试"

    # 抢到锁：置位 busy，表示现在有任务在跑。
    _pi_busy = True
    # 代际 +1，本次任务的“身份号”就是新值。
    _generation += 1
    my_gen = _generation
    try:
        # 先同步跑，最多等 PI_SYNC_TIMEOUT 秒。
        ok, output, timed_out_proc = await _run_pi_sync(task, PI_SYNC_TIMEOUT, my_gen)
    except Exception:
        # 意外异常也要释放锁，避免把后续调用卡死；然后继续向上抛。
        _reset_runtime_state(my_gen)
        raise

    # 哨兵字符串表示同步阶段超时了，进程还在后台跑。
    if output == "__TIMEOUT__":
        # 把还在跑的进程交给后台协程收尾；busy 标志也一并由它持有到结束。
        _spawn_background_wait(timed_out_proc, task, my_gen)
        # 只回一句“正在处理”，让 LLM 尽快转成语音，不要等结果。
        desc = task[:30]
        return f"Pai 正在处理「{desc}」，稍后问我结果"

    # 同步阶段就结束了：先释放锁。
    _reset_runtime_state(my_gen)
    # 同步结果直接返回给 LLM，不入队；pi_check_results 只服务后台任务。

    # 失败直接回错误文本。
    if not ok:
        return f"执行失败: {output}"
    # 输出太长就截短，避免 TTS 念一大段。
    if len(output) > MAX_ASK_OUTPUT_CHARS:
        return output[:ASK_OUTPUT_TRUNCATED] + "\n（结果较长已截短）"
    # 空输出给个占位说明，否则 LLM 没东西可念。
    return output or "（Pi 没有返回内容）"


# 注册 pi_check_results 工具；没有参数，所以 description 由 docstring 自动生成。
@mcp.tool()
async def pi_check_results() -> str:
    """检查 Pi 后台任务是否完成，FIFO 取下一个结果，取完即消。

    为什么 FIFO：先完成的任务先被取走，符合“我上次问的结果”直觉；
    取完就删，避免下次又问到旧结果。
    """
    # 队列为空说明没有后台结果可给。
    if not _completed:
        return "暂时没有完成的任务"

    # popleft 从左边取最早的，天然 FIFO。
    desc, ok, output = _completed.popleft()
    # 状态词。
    status = "成功" if ok else "失败"
    # 拼返回文本：状态 + 任务描述 + 空行 + 输出。
    text = f"[{status}] {desc}\n\n{output or '（无输出）'}"

    # 太长就截短，防止 TTS 读太久。
    if len(text) > MAX_RESULT_CHARS:
        text = text[:RESULT_TRUNCATED] + "\n\n（结果较长，已截短）"
    return text


# 注册 pi_status 工具。
@mcp.tool()
async def pi_status() -> str:
    """查询 Pi 可用性、工作目录，以及待取结果数量。"""
    try:
        # 跑 `pi --version` 探测 pi 是否可用。
        proc = await asyncio.create_subprocess_exec(
            PI_BIN, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PI_WORKDIR,
        )
        # 最多等 5 秒，避免卡住。
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=VERSION_CHECK_TIMEOUT_SECS
        )
        # 退出码非 0 也算异常，直接返回错误。
        if proc.returncode != 0:
            return f"Pai 状态异常：版本检查退出码 {proc.returncode}"
        # 版本号为空时显示“未知”，避免返回一个空串。
        version = stdout.decode(errors="replace").strip() or "未知"
    except FileNotFoundError:
        # pi 命令不存在。
        return f"Pai 不可用：找不到命令 {PI_BIN}"
    except asyncio.TimeoutError:
        # 版本检查超时。
        return "Pai 状态检查超时"
    except Exception as e:
        # 其它异常。
        return f"Pai 状态异常: {e}"

    # 把 busy 布尔转成中文。
    busy = "忙" if _pi_busy else "空闲"
    # 汇总状态：版本、工作目录、忙闲、待取结果数。
    return (
        f"Pai 可用，版本: {version}，工作目录: {PI_WORKDIR}\n"
        f"状态: {busy}, {len(_completed)} 个结果待查看"
    )


# 注册 pi_stop 工具。
@mcp.tool()
async def pi_stop() -> str:
    """终止当前正在执行（含后台运行）的 pi 任务并释放锁，让 Pai 回到空闲。

    用于任务卡住或耗时过长、用户要求中止的场景；重复调用安全。
    """
    # 声明要修改全局变量。
    global _stop_requested_gen

    # 没有正在跑的进程时，直接告诉用户当前没事可做。
    if _current_proc is None:
        return "Pai 当前没有正在执行的任务"

    # 任务描述截前 30 字，用于回执里念给用户听。
    desc = _current_task[:30]

    # 记录“这一代被请求终止”，这样后台收尾时才知道是用户主动停的，
    # 从而返回“任务已被用户终止”而不是“退出码 -9”。
    _stop_requested_gen = _generation
    # 直接杀掉当前进程。
    _kill_process(_current_proc)
    # 释放锁和当前进程引用。
    _reset_runtime_state(_generation)

    # 回执：已终止 + 任务描述。
    return f"已终止「{desc}」，Pai 现在空闲"


# ── 入口 ─────────────────────────────────────────────────────────────

# 仅当直接运行 `python server.py` 时执行；被 import 时跳过。
if __name__ == "__main__":
    # 打印启动配置，方便排障时确认环境变量是否生效。
    logger.info("pi-xiaozhi-mcp server starting")
    logger.info("  PI_BIN=%s", PI_BIN)
    logger.info("  PI_WORKDIR=%s", PI_WORKDIR)
    logger.info("  PI_EXTRA_ARGS=%s", PI_EXTRA_ARGS)
    logger.info("  PI_SYNC_TIMEOUT=%ds", PI_SYNC_TIMEOUT)
    # 进入 MCP stdio 主循环，等待 mcp_bridge 通过 stdin 发 JSON-RPC。
    mcp.run()
