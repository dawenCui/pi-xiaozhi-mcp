"""Pi Agent MCP Server.

把本地 pi agent 包装成 MCP 工具，暴露给小智云 LLM（DeepSeek/Qwen）。
当用户语音指令被 LLM 判定需要 pi 处理时，LLM 通过 WSS MCP endpoint 调用本工具，
本工具 spawn `pi --print` 执行任务，把结果回流给 LLM 让 TTS 朗读。

工具清单:
  pi_task(task)              — 异步执行，立刻回执；结果写 outbox/
  pi_quick(task)             — 同步执行，<=30 秒快速查询
  pi_background(task, summary) — 异步长任务，自定义口语回执
  pi_check_results()         — 取最新 outbox 结果
  pi_abort()                 — 中止当前任务（预留）

环境变量:
  PI_BIN        — pi 命令路径（默认 pi）
  PI_WORKDIR    — pi 工作目录（默认 ~）
  PI_EXTRA_ARGS — pi 额外参数（如 --model deepseek/deepseek-v4-pro）
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

# ── 配置 ─────────────────────────────────────────────────────────────
PI_BIN = os.environ.get("PI_BIN", "pi")
PI_WORKDIR = os.environ.get("PI_WORKDIR", os.path.expanduser("~"))
PI_EXTRA_ARGS = os.environ.get("PI_EXTRA_ARGS", "").split() if os.environ.get("PI_EXTRA_ARGS") else []

# 超时设置
QUICK_TIMEOUT_SECS = 30
TASK_TIMEOUT_SECS = 120
LONG_TIMEOUT_SECS = 300

# 异步结果输出目录
OUTBOX = Path("/tmp/pi-xiaozhi-outbox")
OUTBOX.mkdir(exist_ok=True)
DONE_DIR = OUTBOX / "done"
DONE_DIR.mkdir(exist_ok=True)

# ── 日志（写到 stderr，不污染 stdio MCP 协议）────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("pi-mcp")

# ── MCP server ────────────────────────────────────────────────────────
mcp = MCPServer("pi-xiaozhi-mcp")


async def _run_pi(prompt: str, timeout: int) -> tuple[bool, str]:
    """运行 pi --print，返回 (成功, 输出文本)。"""
    cmd = [PI_BIN, "--print"] + PI_EXTRA_ARGS + [prompt]
    logger.info("pi run: %s", " ".join(cmd[:4]))
    logger.info("pi input:\n%s", prompt)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PI_WORKDIR,
            env={**os.environ},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                proc.kill()
            logger.warning("pi timeout (>%ds)", timeout)
            return False, f"任务超时（>{timeout}秒），请用异步模式"

        if proc.returncode != 0:
            err = (stderr or b"").decode(errors="replace")[-500:]
            logger.error("pi exit %d: %s", proc.returncode, err)
            return False, f"pi 退出码 {proc.returncode}: {err}"

        text = (stdout or b"").decode(errors="replace").strip()
        logger.info("pi output:\n%s", text)
        return True, text

    except FileNotFoundError:
        return False, f"找不到 pi 命令: {PI_BIN}，请检查 PI_BIN 环境变量"
    except Exception as e:
        logger.exception("pi run failed")
        return False, f"调用失败: {type(e).__name__}: {e}"


def _save_result(task: str, ok: bool, output: str) -> Path:
    """保存异步任务结果到 outbox。"""
    ts = int(time.time() * 1000)
    out_file = OUTBOX / f"{ts}.txt"
    status = "OK" if ok else "FAIL"
    out_file.write_text(f"[{status}] {task}\n\n{output}", encoding="utf-8")
    logger.info("result saved: %s -> %s", task[:50], out_file.name)
    return out_file


# ── 工具定义 ──────────────────────────────────────────────────────────

@mcp.tool()
async def pi_task(task: str) -> str:
    """调用本地 Pi Agent 执行任务（异步模式）。

    本工具**异步执行**：立刻返回口语回执，实际任务在后台跑。
    结果写到 /tmp/pi-xiaozhi-outbox/，用户稍后问"结果呢"时调 pi_check_results 取。

    适用场景：
    1. 需要 Pi 执行的复杂指令（写代码、分析文件、搜索资料）
    2. 耗时较长的任务（可能几十秒到几分钟）
    3. 需要 Pi 的工具能力（bash、文件读写、代码编辑）

    简单快速查询用 pi_quick。

    重要：调用后告诉用户"Pai 正在处理，稍等一下问结果"。

    Args:
        task: 给 Pi 的中文指令，越具体越好。例:
            "帮我查一下今天的天气"
            "写一篇关于 AI 的 200 字短文"
            "看看当前目录有什么文件"
    """
    async def _bg():
        ok, output = await _run_pi(task, LONG_TIMEOUT_SECS)
        _save_result(task, ok, output)

    asyncio.create_task(_bg())
    # 截断任务描述用于回执
    desc = task[:30] if len(task) > 30 else task
    return f"Pai 正在处理「{desc}」，稍等一下问我结果"


@mcp.tool()
async def pi_quick(task: str) -> str:
    """调用本地 Pi Agent 执行快速查询（同步模式，<=30 秒）。

    适用于简单、预计能快速完成的任务。如果超时，返回提示让用户改用 pi_task 异步模式。

    Args:
        task: 简短具体的查询指令。例:
            "现在几点了"
            "1+1等于几"
            "回复两个字：收到"
    """
    ok, output = await _run_pi(task, QUICK_TIMEOUT_SECS)
    if not ok:
        if "超时" in output:
            return "这个任务比较慢，请用 pi_task 工具异步执行"
        return f"执行失败: {output}"

    # 截断过长结果（TTS 不适合念太长）
    if len(output) > 500:
        return output[:400] + "\n（结果较长已截短）"
    return output or "（Pi 没有返回内容）"


@mcp.tool()
async def pi_background(task: str, summary_for_user: str) -> str:
    """调用本地 Pi Agent 执行长任务（异步模式，自定义回执）。

    与 pi_task 类似，但允许 LLM 自定义对用户的口语化回执，
    适合写文章、深度研究等耗时较长的场景。

    Args:
        task: 给 Pi 的指令。
        summary_for_user: 对用户口语化的回执。例:
            "Pai 这就去写文章，写完叫你"
            "收到，Pai 正在深度研究这个话题"
    """
    async def _bg():
        ok, output = await _run_pi(task, LONG_TIMEOUT_SECS)
        _save_result(task, ok, output)

    asyncio.create_task(_bg())
    return summary_for_user


@mcp.tool()
async def pi_check_results() -> str:
    """检查 Pi 后台任务有没有出结果。

    返回最近一个已完成的任务结果。读完后标记为已读，避免重复播报。
    如果没有完成的任务，返回"暂时没有完成的任务"。
    """
    files = sorted(OUTBOX.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    if not files:
        return "暂时没有完成的任务"

    # 取最近一个
    latest = files[-1]
    text = latest.read_text(encoding="utf-8", errors="replace")

    # 读完移到 done/ 避免重复念
    latest.rename(DONE_DIR / latest.name)

    # 截断过长结果
    if len(text) > 800:
        text = text[:600] + "\n\n（结果较长，已截短）"
    return text


@mcp.tool()
async def pi_status() -> str:
    """查询 Pi Agent 的当前状态。

    返回 Pi 是否可用、工作目录、模型信息等。
    用于用户问"Pai 在吗"、"Pai 状态怎么样"等场景。
    """
    # 简单检查 pi 命令是否可用
    try:
        proc = await asyncio.create_subprocess_exec(
            PI_BIN, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PI_WORKDIR,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        version = stdout.decode(errors="replace").strip()
        return f"Pai 可用，版本: {version}，工作目录: {PI_WORKDIR}"
    except FileNotFoundError:
        return f"Pai 不可用：找不到命令 {PI_BIN}"
    except asyncio.TimeoutError:
        return "Pai 状态检查超时"
    except Exception as e:
        return f"Pai 状态异常: {e}"


# ── 入口 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("pi-xiaozhi-mcp server starting")
    logger.info("  PI_BIN=%s", PI_BIN)
    logger.info("  PI_WORKDIR=%s", PI_WORKDIR)
    logger.info("  PI_EXTRA_ARGS=%s", PI_EXTRA_ARGS)
    logger.info("  OUTBOX=%s", OUTBOX)
    mcp.run()
