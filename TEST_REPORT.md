# pi-xiaozhi-mcp 测试报告

**日期**: 2026-09-29
**项目路径**: `/Users/dajun/Git/pi-xiaozhi-mcp`
**Python 版本**: 3.14.7
**依赖版本**: mcp 2.2.0, websockets 17.1, python-dotenv 1.2.3, pytest 9.1.1, pytest-asyncio 1.4.0

---

## 1. 测试套件概览

测试位于 `bridge/tests/`，共 **58 个用例，全部通过**。

| 文件 | 用例数 | 说明 |
|---|---|---|
| `test_server.py` | 35 | MCPServer 工具逻辑（pi_ask / pi_check_results / pi_status / pi_stop） |
| `test_race_condition.py` | 3 | 并发调用与 `_pi_busy` 原子性 |
| `test_mcp_bridge.py` | 20 | WSS ↔ stdio 桥接（配置加载、命令构建、管道、重连、信号、环境变量） |

```bash
cd bridge && .venv/bin/python -m pytest tests/ -v
# 58 passed
```

---

## 2. 修复记录

### 2.1 真实代码 Bug（由新增测试发现并已修复）

`bridge/mcp_bridge.py` 的 `connect_with_retry()` 在断线重连分支调用了
**未定义**的 `log_bridge_event()`，会导致断线后重连循环直接 `NameError` 崩溃。

```python
# 修复前（第 152 行）
log_bridge_event("disconnect", f"[{target}] 连接断开: {e}")  # NameError!

# 修复后：补上函数定义（后续代码简化时该包装已移除，见 2.7）
def log_bridge_event(event: str, message: str) -> None:
    """记录桥接事件到日志。"""
    logger.info("[event:%s] %s", event, message)
```

对应测试：`tests/test_mcp_bridge.py::test_connect_with_retry_backoff_and_retry`

### 2.2 过期测试修复（测试与实现不一致）

| 测试 | 原问题 | 修复 |
|---|---|---|
| `test_pi_ask_busy_rejection` | 通过 `_pi_lock.acquire()` 模拟忙，但当前实现用 `_pi_busy` 标志，导致测试**死锁挂起** | 改为设置 `_pi_busy = True` |
| `test_pi_status_reports_busy_state` | 同上，断言永远失败 | 改为设置 `_pi_busy = True` |
| `test_race_condition.py`（2 个） | 引用不存在的 `server._execute_pi`（真实函数是 `_run_pi_sync`），`AttributeError` | 重写为针对 `_run_pi_sync` / `_pi_busy` 的并发测试 |

### 2.3 并发互斥改为「全程互斥」

原实现：`_pi_busy` 只在同步等待窗口内保持，超时转后台后立即复位，
导致后台进程仍在运行时新指令仍会启动第二个 `pi`（有状态，会互相污染）。

修复后：`_pi_busy` 覆盖「同步等待 + 后台等待」整个进程生命周期，
后台任务在 `finally` 中才释放标志；同时移除了冗余的 `_pi_lock`，
`_pi_busy` 成为唯一互斥标志。

对应测试：`tests/test_server.py::test_pi_ask_rejected_while_background_running`

### 2.4 稳定性修复（代码审查后，第二轮）

| 问题 | 修复 | 对应测试 |
|---|---|---|
| 过期后台任务会覆盖新任务状态（竞态） | 引入任务代际 `_generation`；`_reset_runtime_state(gen)` 仅在 `gen == _generation` 时复位；`_stop_requested` 改为代际化的 `_stop_requested_gen` + `_is_stopped(gen)` | `test_reset_runtime_state_ignores_stale_generation`、`test_stale_background_waiter_does_not_clobber_new_task` |
| 后台 task 引用未保存，可能被 GC | `_spawn_background_wait` 把 `create_task` 结果保存到 `_background_tasks`，`add_done_callback(discard)` 自动清理 | `test_background_task_reference_is_kept` |
| 重连退避只增不减，长期运行锁死在 600s | `connect_to_server` 新增 `on_connected` 回调，连上 WebSocket 后复位 `backoff` / `reconnect_attempt` | `test_connect_with_retry_resets_backoff_after_connect` |
| stderr 独立句柄与 `RotatingFileHandler` 轮转/并发写冲突 | `pipe_process_stderr_to_log` 改走 `logger.info`，统一由 `RotatingFileHandler` 管理 | `test_pipe_process_stderr_to_log`（改为断言 logger 输出） |
| 同步结果也进入 FIFO 队列，`pi_check_results` 会重复念 | 同步路径不再 `_complete_task`，队列只存后台结果 | `test_pi_ask_sync_result_not_enqueued` |
| `pi_status` 不检查退出码 / 空版本 | `--version` 非零退出返回异常提示；空版本显示「未知」 | `test_pi_status_nonzero_exit`、`test_pi_status_empty_version` |

### 2.5 健壮性修复（第三轮）

| 问题 | 修复 | 对应测试 |
|---|---|---|
| 完整 prompt 明文写日志（含 `pi run` 行第 4 个 token） | 命令日志改为 `cmd[:-1]`（不含 prompt）；prompt 只写前 200 字符预览，完整内容降到 DEBUG | `test_run_pi_sync_truncates_logged_prompt` |
| `load_config` 顶层不是对象时 `_main` 直接 `cfg.get` 崩溃 | `load_config` 校验 `isinstance(data, dict)`，非对象返回 `{}` | `test_load_config_json_not_object` |
| 只处理 SIGINT，launchd 停止时发 SIGTERM | 抽出 `register_signal_handlers()`，同时注册 SIGINT/SIGTERM | `test_register_signal_handlers` |
| `PI_EXTRA_ARGS` 用 `.split()` 不支持引号 | 改用 `shlex.split`（`_parse_extra_args`） | `test_parse_extra_args_supports_quoting` |
| `PI_SYNC_TIMEOUT` 无下限校验 | 抽出 `_env_int`，非法值回退默认、下限 1 | `test_env_int_clamps_and_falls_back` |
| 二次 `communicate()` 依赖现代 asyncio | 代码注释说明 + 真实子进程集成测试锁定行为 | `test_communicate_retry_after_timeout` |

### 2.6 依赖与环境变量加载（第四轮）

| 问题 | 修复 | 对应测试 |
|---|---|---|
| `load_dotenv()` 在 import 时副作用，污染进程环境 | 抽出 `load_env()`，只在 `__main__` 启动时按需调用 | `test_load_env_reads_env_file` |
| `requirements.txt` 写 `mcp[cli]>=1.0`，实际依赖 mcp 2.x | 收紧为 `mcp[cli]>=2.0` | 无（依赖声明） |

### 2.7 代码简化（第五轮）

本轮只做可读性简化，不改变行为、不新增架构：

| 位置 | 简化 |
|---|---|
| `server.py` `_run_pi_sync` / `_wait_for_proc` | 提取共享的 `_decode_result()`，去掉两处重复的「退出码检查 + stdout/stderr 解码」 |
| `server.py` `pi_ask` / `pi_stop` | `task[:30] if len(task) > 30 else task` 简化为 `task[:30]` |
| `mcp_bridge.py` | 移除只调一次、且与 `logger.warning` 重复的 `log_bridge_event()` 包装 |
| `mcp_bridge.py` `build_server_command` | 去掉多余的 `isinstance(cfg, dict)`（`load_config` 已保证返回 dict） |
| `mcp_bridge.py` `_main` | 去掉 `all_names = list(...)`，直接推导 `skipped` |

---

## 3. 工具注册情况（与旧报告的重要差异）

旧报告记录 5 个工具（`pi_task` / `pi_quick` / `pi_background` 等），
**当前实际注册了 4 个工具**（已通过 `test_registered_tool_names` 验证）：

| 工具名 | 模式 | 说明 |
|---|---|---|
| `pi_ask(task)` | 同步优先，超时转异步 | 统一入口（同步等待默认 4 秒，可配 `PI_SYNC_TIMEOUT`） |
| `pi_check_results()` | 同步 | FIFO 取下一个后台完成结果 |
| `pi_status()` | 同步 | Pi 可用性 + 队列状态 |
| `pi_stop()` | 同步 | 终止当前任务并释放锁 |

> 注：`pi_task` / `pi_quick` / `pi_background` 已被合并为统一的 `pi_ask`。
> `bridge/mcp_bridge.py` 顶部注释里的旧工具名也已同步更正。
>
> `pi_stop` 用于杀死长时间未完成的任务（同步或后台阶段均可），避免互斥锁被长期占用。
>
> `pi_ask` 的同步窗口默认值从 10 秒降到 4 秒，让小智设备在语音回复时限内拿到「正在处理」回执（见 README）。

---

## 4. 测试明细

### 4.1 `test_mcp_bridge.py`（累计 20 个）

- `load_config`：文件缺失 / 非法 JSON / 正常加载 / 顶层非对象
- `load_env`：按需加载 .env
- `build_server_command`：
  - stdio 命令 + 子进程 env 注入
  - `disabled` 配置抛错
  - 缺 `command` 字段抛错
  - http/streamablehttp 走 `mcp_proxy --transport streamablehttp` + headers
  - sse 走 `mcp_proxy`（不带 `--transport`）
  - 向后兼容脚本路径回退
  - 未知 target 抛错
  - 不支持类型抛错
- 管道函数：
  - `pipe_process_to_websocket` 逐行发送
  - `pipe_websocket_to_process` 写入 stdin 并 flush、bytes 解码、异常关闭
  - `pipe_process_stderr_to_log` 通过 logger 写日志
- 连接生命周期：
  - `connect_to_server` 正确启动子进程、双向管道、退出时 terminate/wait
  - `connect_with_retry` 失败后指数退避并重连
  - `connect_with_retry` 连接成功后复位退避
- 信号处理：
  - `register_signal_handlers` 注册 SIGINT + SIGTERM

### 4.2 `test_server.py`（累计 35 个）

- `pi_ask`：pi 命令不存在、非零退出码、空输出、长输出截断
- `pi_ask`：超时转后台后 busy 保持、新指令被拒绝（全程互斥）
- `pi_ask`：同步结果不入队
- `pi_check_results`：FIFO 消费、长结果截断
- `pi_status`：命令不存在、版本检查超时、非零退出码、空版本、待取结果计数
- `_run_pi_sync`：超时返回 `__TIMEOUT__` 且不杀进程；长 prompt 日志截断
- `_wait_for_proc`：正常结束 / 非零退出
- `pi_stop`：无任务时返回提示；终止后台任务并产出「已终止」结果
- 代际防护：过期代际复位被忽略；过期后台收尾不覆盖新任务状态
- 后台任务：task 引用保存与自动移除
- 参数解析：`_parse_extra_args` 引号支持、`_env_int` 下限与回退
- 集成：真实子进程验证二次 `communicate()` 可重试
- 工具注册：验证恰好暴露 4 个工具 + 描述非空

### 4.3 `test_race_condition.py`（3 个）

- 3 个并发 `pi_ask` 只有 1 个真正执行，其余返回"忙"
- 任务完成后 busy 标志复位，后续调用可继续
- `_run_pi_sync` 抛异常时 busy 标志仍被复位

---

## 5. 测试结果总览

| 测试项 | 结果 |
|---|---|
| 全部测试 | ✅ 58/58 通过 |
| server.py 语法检查 | ✅ py_compile 通过 |
| mcp_bridge.py 语法检查 | ✅ py_compile 通过 |
| 工具注册验证 | ✅ 4 个工具（pi_ask / pi_check_results / pi_status / pi_stop） |
| 并发安全（busy 标志 + 代际防护） | ✅ 通过 |
| 后台任务生命周期（引用保持 + 收尾） | ✅ 通过 |
| mcp_bridge 配置/命令/管道/重连/信号/环境变量 | ✅ 通过 |
| 参数解析 / 日志隐私 / 二次 communicate | ✅ 通过 |
| 发现的真实 Bug | ✅ 已修复（log_bridge_event 未定义 + 第二轮 6 项 + 第三轮 6 项 + 第四轮 2 项） |
| 代码简化（第五轮） | ✅ 已完成（去重 / 去冗余，行为不变） |

---

## 6. 遗留建议

本轮已处理完毕，无阻塞项。
