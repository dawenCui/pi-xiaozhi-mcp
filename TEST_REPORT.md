# pi-xiaozhi-mcp 测试报告

**日期**: 2026-09-29
**项目路径**: `/Users/dajun/Git/pi-xiaozhi-mcp`
**Python 版本**: 3.14
**依赖版本**: mcp 2.2.0, websockets 17.1, python-dotenv 1.2.3

---

## 1. 项目结构

```
pi-xiaozhi-mcp/
├── .gitignore                          ✅ 排除 .env, mcp_config.json, venv
├── README.md                           ✅ 含完整使用说明
├── TEST_REPORT.md                      ✅ 本测试报告
├── bridge/
│   ├── .env.example                    ✅ WSS endpoint + pi 配置模板
│   ├── mcp_bridge.py                   ✅ 279 行，WSS ↔ stdio 桥接
│   ├── mcp_config.json.example         ✅ MCP server 配置模板
│   ├── requirements.txt                ✅ 3 个依赖
│   └── server.py                       ✅ 245 行，MCPServer 工具服务
└── launchd/
    └── com.example.pi-xiaozhi-mcp.plist  ✅ macOS 守护进程模板
```

**总计**: 8 个文件，~723 行核心代码（server.py + mcp_bridge.py + README.md）

---

## 2. 语法检查

| 文件 | 结果 |
|---|---|
| bridge/server.py | ✅ py_compile 通过 |
| bridge/mcp_bridge.py | ✅ py_compile 通过 |

---

## 3. 依赖安装

```
pip install -r requirements.txt
```

| 包 | 版本 | 状态 |
|---|---|---|
| mcp[cli] | 2.2.0 | ✅ 已安装 |
| websockets | 17.1 | ✅ 已安装 |
| python-dotenv | 1.2.3 | ✅ 已安装 |

**注意**: mcp 2.x 将 `FastMCP` 重命名为 `MCPServer`，代码已适配。

---

## 4. server.py 工具注册测试

```python
from mcp.server.mcpserver import MCPServer
# 导入 server.py 后检查注册的工具
```

| 工具名 | 模式 | 超时 | 状态 |
|---|---|---|---|
| `pi_task` | 异步 | 5 分钟 | ✅ 已注册 |
| `pi_quick` | 同步 | 30 秒 | ✅ 已注册 |
| `pi_background` | 异步 | 5 分钟 | ✅ 已注册 |
| `pi_check_results` | 同步 | - | ✅ 已注册 |
| `pi_status` | 同步 | 5 秒 | ✅ 已注册 |

**结果**: 5/5 工具全部注册成功

---

## 5. mcp_bridge.py 模块测试

| 函数 | 存在 | 说明 |
|---|---|---|
| `connect_with_retry()` | ✅ | 指数退避无限重连 |
| `connect_to_server()` | ✅ | WSS 连接 + 启动子进程 + 双向管道 |
| `build_server_command()` | ✅ | 从 mcp_config.json 构建命令 |
| `load_config()` | ✅ | 加载 JSON 配置 |
| `pipe_websocket_to_process()` | ✅ | WSS → stdin |
| `pipe_process_to_websocket()` | ✅ | stdout → WSS |
| `pipe_process_stderr_to_terminal()` | ✅ | stderr → 终端 |

`load_config()` 在无 mcp_config.json 时正确返回空 `{}`。

---

## 6. 敏感信息检查

```bash
grep -rn "/Users/dajun\|token=\|password\|secret\|api_key\|192\.168\.\|10\.0\." .
```

| 文件 | 发现 | 处理 |
|---|---|---|
| server.py:16 | `/Users/dajun/Opt/pi_workspace` | ✅ 已改为 `~` |
| mcp_bridge.py:3 | `token=...` | ✅ 文档示例，安全 |
| mcp_bridge.py:145 | `?token=***` | ✅ 日志脱敏 |
| .env.example:3 | `YOUR_TOKEN_HERE` | ✅ 占位符 |
| .env.example:9 | `/Users/you/...` | ✅ 占位符 |
| README.md | `token=...` | ✅ 文档示例，安全 |

**结果**: 无敏感信息泄漏

---

## 7. 配置模板检查

| 文件 | 检查项 | 状态 |
|---|---|---|
| .env.example | MCP_ENDPOINT 占位符 | ✅ `YOUR_TOKEN_HERE` |
| .env.example | PI_BIN / PI_WORKDIR / PI_EXTRA_ARGS 注释 | ✅ |
| mcp_config.json.example | 路径占位符 | ✅ `/PATH/TO/pi-xiaozhi-mcp` |
| plist | 路径占位符 | ✅ `/PATH/TO/pi-xiaozhi-mcp` |
| plist | StandardOutPath / StandardErrorPath | ✅ `/dev/null`（日志由 RotatingFileHandler 管理） |

---

## 8. MCP 工具对比（stackchan-claude-bridge vs pi-xiaozhi-mcp）

### stackchan-claude-bridge（4 个工具）

| 工具名 | 参数 | 模式 | 超时 | 说明 |
|---|---|---|---|---|
| `claude_code` | `task: str` | 异步 | 5 分钟 | 通用任务，立刻回执"派蒙正在查" |
| `claude_code_quick` | `task: str` | 同步 | 20 秒 | 快速查询，直接返回结果 |
| `claude_code_background` | `task: str, summary_for_user: str` | 异步 | 5 分钟 | 长任务，LLM 自定义口语回执 |
| `claude_code_check_results` | 无 | 同步 | - | 取最新 outbox 结果，移到 done/ |

**底层命令**: `claude --print --permission-mode acceptEdits <prompt>`
**结果目录**: `/tmp/claude-bridge-outbox/`

### pi-xiaozhi-mcp（5 个工具）

| 工具名 | 参数 | 模式 | 超时 | 说明 |
|---|---|---|---|---|
| `pi_task` | `task: str` | 异步 | 5 分钟 | 通用任务，立刻回执"Pai 正在处理" |
| `pi_quick` | `task: str` | 同步 | 30 秒 | 快速查询，直接返回结果 |
| `pi_background` | `task: str, summary_for_user: str` | 异步 | 5 分钟 | 长任务，LLM 自定义口语回执 |
| `pi_check_results` | 无 | 同步 | - | 取最新 outbox 结果，移到 done/ |
| `pi_status` | 无 | 同步 | 5 秒 | 检查 pi 是否可用，返回版本和工作目录 |

**底层命令**: `pi --print --no-session [PI_EXTRA_ARGS] <prompt>`
**结果目录**: `/tmp/pi-xiaozhi-outbox/`

### 差异分析

| 对比项 | stackchan-claude-bridge | pi-xiaozhi-mcp |
|---|---|---|
| 工具数量 | 4 | **5**（多 `pi_status`） |
| 同步查询超时 | 20 秒 | **30 秒**（更宽松） |
| 状态检查工具 | 无 | **有** `pi_status` |
| 底层 CLI | `claude --print` | `pi --print --no-session` |
| 权限模式 | `--permission-mode acceptEdits` | 无（pi 默认） |
| 额外参数 | 无 | 支持 `PI_EXTRA_ARGS` 环境变量 |
| 结果截断 | 800 字 → 600 字 | 800 字 → 600 字（一致） |
| 快速结果截断 | 500 字 → 400 字 | 500 字 → 400 字（一致） |

**关键差异**:
1. `pi_status` 是 pi-xiaozhi-mcp 独有的工具，允许 LLM 在执行任务前检查 Pi 是否可用，避免盲目调用
2. `pi_quick` 超时从 20s 放宽到 30s，因为 pi CLI 启动可能比 claude 稍慢
3. `pi --no-session` 禁用 session 持久化，每次调用独立，避免上下文污染
4. `PI_EXTRA_ARGS` 允许运行时注入额外参数（如 `--model`），灵活性更高

---

## 9. 架构对比（整体设计）

| 特性 | stackchan-claude-bridge | pi-xiaozhi-mcp |
|---|---|---|
| 桥接方式 | WSS ↔ stdio | WSS ↔ stdio ✅ |
| MCP 框架 | FastMCP (mcp 1.x) | MCPServer (mcp 2.x) ✅ |
| 工具数量 | 4 个 | 5 个（多 pi_status） ✅ |
| 异步模式 | ✅ | ✅ |
| 自动重连 | ✅ 指数退避 | ✅ 指数退避 |
| 配置方式 | mcp_config.json | mcp_config.json ✅ |
| 守护进程 | launchd | launchd ✅ |
| 文档 | README.md | README.md ✅ |

---

## 10. 总结

| 测试项 | 结果 |
|---|---|
| 项目结构完整性 | ✅ 通过 |
| Python 语法检查 | ✅ 通过 |
| 依赖安装 | ✅ 通过 |
| 工具注册 | ✅ 5/5 通过 |
| 模块函数完整性 | ✅ 通过 |
| 敏感信息检查 | ✅ 通过 |
| 配置模板 | ✅ 通过 |

**整体评估**: 项目结构完整，代码质量良好，可以投入使用。

---

## 11. 后续步骤

1. **配置 WSS endpoint**: 复制 `.env.example` → `.env`，填入从 xiaozhi.me 获取的 WSS URL
2. **配置 MCP server 路径**: 复制 `mcp_config.json.example` → `mcp_config.json`，替换 `/PATH/TO/` 为实际路径
3. **手动测试**: `python mcp_bridge.py` 验证连接
4. **安装 launchd 守护**: 复制 plist 到 `~/Library/LaunchAgents/`，修改路径后 `launchctl load`
5. **推送到 GitHub**: `git remote add origin <url> && git push -u origin main`
