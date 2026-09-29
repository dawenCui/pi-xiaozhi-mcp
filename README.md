# Pi × 小智 MCP Bridge

把本地 **Pi Agent**（Qoder CLI）变成小智桌面机器人的云端 MCP 工具。

- 🎤 跟小智设备说话（DeepSeek/Qwen 做主对话，~1 秒响应）
- 🤖 需要 Pi 能力时，LLM 通过 WSS MCP 调你 Mac 上的 `pi --print`
- 🎯 异步模式：长任务超时自动转后台，结果稍后取

> "Pai，帮我查一下今天的天气" → DeepSeek 看到 `pi_ask` 工具 → WSS 调到你 Mac → spawn `pi --print` → 结果回 TTS 念出来

---

## 这是什么

[小智 AI](https://github.com/78/xiaozhi-esp32) 是开源桌面语音助手，支持 MCP 工具扩展。
[虾哥云平台](https://xiaozhi.me) 提供 MCP 接入点（WSS），让自定义 MCP server 暴露给 LLM。

本项目：
- ✅ 通过 xiaozhi.me 的 WSS MCP 接入点给 LLM 加了 `pi_ask` / `pi_check_results` / `pi_status` / `pi_stop` 工具
- ✅ 工具通过 WSS 路由到你 Mac 上的本地 bridge
- ✅ 本地 bridge spawn `pi --print` 跑 Pi Agent
- ✅ 异步模式：4 秒内出结果直接念，超时回"Pai 正在处理"，结果写队列，用户问"结果呢"再取
- ✅ 自动重连（指数退避 1s → 600s，连接成功后复位）

架构：

```
你说「Pai，帮我写一篇关于 X 的文章」
                ↓
        小智设备麦克风 ── 流式 ASR
                ↓
        虾哥后端 api.xiaozhi.me + DeepSeek V4
                ↓ 看到 pi_ask 工具就 tools/call
        wss://api.xiaozhi.me/mcp/?token=...
                ↓
        你 Mac 上的 mcp_bridge.py（WSS ↔ stdio）
                ↓
        server.py (MCPServer, 4 个工具)
                ↓ subprocess
        pi --print "task"
                ↓
        4 秒内完成 → 直接返回结果
        超时 → 返回 "Pai 正在处理…，稍后问我结果"
                ↓
        TTS → 小智说话
        ......稍后......
你说「Pai，结果呢」
                ↓
        pi_check_results 取下一个后台结果
                ↓
        TTS 念结果（自动截到 600 字）
```

---

## 适合谁？

- 已经有小智设备（Stack-chan / ESP32-S3 等）并接入了 xiaozhi.me
- Mac 上装了 Pi CLI（`pi` 命令可用）
- 想让小智设备调用 Pi 的能力（bash、文件读写、代码、MCP 工具等）

**不需要**：
- 自己部署 xiaozhi-server（用虾哥免费云后端）
- 装 Home Assistant 或任何重型中间件
- 改固件（小智官方固件即可）

---

## 快速上手

### 1. 注册小智账号 + 创建智能体

1. 打开 [xiaozhi.me](https://xiaozhi.me)，手机号注册
2. 控制台 → 智能体 → +添加，创建智能体（比如叫"Pai"）
3. 智能体配置：
   - **语言模型**：选 `DeepSeek V4（性格丰富）`
   - **角色介绍**：见下面 [推荐角色介绍](#推荐角色介绍)
   - **MCP 设置 → 自定义服务**：点 `[获取 MCP 接入点]`，复制 `wss://api.xiaozhi.me/mcp/?token=...` URL
   - 保存

### 2. 部署 Mac 上的 bridge

```bash
# clone 本项目
git clone https://github.com/YOUR_USERNAME/pi-xiaozhi-mcp.git
cd pi-xiaozhi-mcp/bridge

# 装 Python 依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 配置 WSS endpoint
cp .env.example .env
vim .env    # 把 MCP_ENDPOINT 改成你的 WSS URL

# 配置 MCP server 路径
cp mcp_config.json.example mcp_config.json
# 把 /PATH/TO/ 替换成实际绝对路径
sed -i '' "s|/PATH/TO/pi-xiaozhi-mcp|$(cd ..; pwd)|g" mcp_config.json

# 手动跑一下验证
python mcp_bridge.py
# 应该看到：
#   "[pi_xiaozhi] 连接 WebSocket: ..."
#   "[pi_xiaozhi] WebSocket 连接成功"
#   "[pi_xiaozhi] 启动 MCP server: ..."
#   "pi-xiaozhi-mcp server starting"
# Ctrl+C 退出
```

### 3. 装 launchd 守护（7×24 在线）

```bash
# 复制 plist 模板
cp launchd/com.example.pi-xiaozhi-mcp.plist ~/Library/LaunchAgents/

# 编辑 plist 把路径改成实际的
vim ~/Library/LaunchAgents/com.example.pi-xiaozhi-mcp.plist

# 加载
launchctl load -w ~/Library/LaunchAgents/com.example.pi-xiaozhi-mcp.plist

# 验证
launchctl list | grep pi-xiaozhi
tail -f /tmp/pi-xiaozhi-bridge.log
```

### 4. 开始用

| 你说 | 效果 |
|---|---|
| Pai，1+1 等于几 | `pi_ask` 同步出结果，直接念结果 |
| Pai，帮我查一下今天的天气 | `pi_ask` 执行，超时自动转后台 |
| Pai，结果呢 | `pi_check_results` 取下一个后台结果念出来 |
| Pai，写一篇 500 字关于 AI 的文章 | `pi_ask`，超时回“正在处理”，写完再问结果 |
| Pai 在吗 | `pi_status` 检查 Pi 是否可用 |

---

## 推荐角色介绍

xiaozhi.me 控制台 → 智能体配置 → 角色介绍里贴这段：

```
我叫Pai，桌面陪伴AI，活泼可爱、口语自然、回复1-2句话不超过50字。
能力：
- 简单问答、闲聊、查时间天气：直接回答
- 需要执行任务、查资料、写代码、分析文件、写文章：调 pi_ask 工具执行
- 用户问“结果呢”“查到了吗”“写完了吗”：调 pi_check_results
- 用户问“Pai在吗”“Pai状态”：调 pi_status
- 任务卡住或用户要求中止：调 pi_stop
回复必须是纯口语文字，禁止 markdown、列表、emoji，因为内容会被语音朗读，格式符号会干扰听感。
```

---

## 工具说明

| 工具 | 模式 | 超时 | 场景 |
|---|---|---|---|
| `pi_ask` | 同步优先 | 4 秒（可配） | 统一入口，4 秒内出结果直接念；超时回“正在处理”，后台继续 |
| `pi_check_results` | 同步 | - | FIFO 取下一个后台完成结果（取完即消） |
| `pi_status` | 同步 | 5 秒 | 检查 Pi 是否可用、队列状态 |
| `pi_stop` | 同步 | - | 终止当前任务并释放锁 |

---

## 环境变量

| 变量 | 必须 | 说明 |
|---|---|---|
| `MCP_ENDPOINT` | 是 | 从 xiaozhi.me 获取的 WSS URL |
| `PI_BIN` | 否 | pi 命令路径（默认 `pi`） |
| `PI_WORKDIR` | 否 | pi 工作目录（默认 `~`） |
| `PI_EXTRA_ARGS` | 否 | pi 额外参数（如 `--model xxx`，支持引号/空格） |
| `PI_SYNC_TIMEOUT` | 否 | `pi_ask` 同步等待秒数，超时自动转后台（默认 `4`，需明显低于 10 秒以保住语音回复） |
| `MCP_CONFIG` | 否 | mcp_config.json 路径（默认 `./mcp_config.json`） |

---

## 限制

- **小智设备只能绑一个智能体** —— 绑给"Pai"后，想换别的智能体需要在 xiaozhi.me 重新切换
- **Pi 任务有延迟** —— 4 秒内完成的任务直接念结果；更长任务在约 4 秒后回"正在处理"，之后再加实际执行时间
- **Mac 必须开机** —— bridge 需要本机进程 spawn `pi` 命令
- **不能主动通知** —— 小智设备和 WSS bridge 都不支持主动推送，只能用户问"结果呢"时取

---

## 致谢

- 桥接模式参考 [heavenchenggong/stackchan-claude-bridge](https://github.com/heavenchenggong/stackchan-claude-bridge)
- 上游固件 [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)
- 虾哥云平台 [xiaozhi.me](https://xiaozhi.me)

## License

MIT
