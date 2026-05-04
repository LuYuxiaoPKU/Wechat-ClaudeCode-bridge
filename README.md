# WeChat-Claude-Bridge

通过微信控制 [Claude Code](https://claude.ai/code) CLI，在任何地方用微信消息指挥 Claude 写代码、操作文件、执行命令。

Control Claude Code CLI via WeChat messages. Code, debug, and manage files from anywhere using WeChat.

> Written by **Claude Code** (Opus 4.7) + **DeepSeek V4 Pro[1m]**
> 本项目由 Claude Code + DeepSeek V4 Pro[1m] 编写

```
+--------+     iLink API      +------------------+   ThreadPool  +-------------+
|  WeChat | <===============> | wechat-claude-   | <===========> | Claude      |
|  Client |    long polling   | bridge (Python)  |  subprocess   | Code CLI    |
+--------+                    +------------------+               +-------------+
                                      |  |
                                      |  | Web console (:9876)
                                      v  v
                               +-------------+  +-------------+
                               | Per-user cwd|  | Logs/History|
                               +-------------+  +-------------+
```

## Features / 功能

- **微信 ↔ Claude Code 双向桥接** — 微信消息转发给 Claude Code，回复返回微信
- **并发处理** — ThreadPoolExecutor，多个用户同时提问互不阻塞
- **流式输出** — Claude 思考期间持续推送增量文本，不再长时间空白等待
- **多轮对话** — 基于 `--resume`/`--session-id` 保持会话上下文
- **独立工作目录** — 每个微信用户可指定不同项目目录（`/cwd` 命令）
- **模型切换** — 微信内切换 `opus`/`sonnet`/`haiku`（`/model` 命令）
- **命令执行** — `/exec` 在项目目录直接执行 shell 命令
- **长消息拆分** — 超长回复自动按段落拆分为多条微信消息
- **速率限制** — 防止刷屏，默认 5s 间隔
- **用户白名单** — `WCB_ALLOWED_USERS` 环境变量控制访问权限
- **优雅退出** — Ctrl+C 退出时自动通知所有用户
- **Web 控制台** — `http://127.0.0.1:9876` 查看运行状态
- **消息历史** — 自动记录到 `~/.wechat-claude-bridge/history/`
- **文件日志** — 自动轮转日志 `~/.wechat-claude-bridge/bridge.log`
- **纯 ASCII 输出** — 无 emoji，兼容 MobaXterm 等终端

## Quick Start / 快速开始

### 前提

- Python 3.9+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装
- 微信 iLink Bot

### 安装

```bash
git clone <repo-url>
cd wechat-claude-bridge
pip install -r requirements.txt
```

### 运行

```bash
# 首次运行 — 扫码登录
python3 bridge.py --login

# 后续运行 — 自动复用 token
python3 bridge.py
```

### Docker

```bash
# 首次登录
docker-compose run --rm bridge python3 bridge.py --login

# 后台运行
docker-compose up -d
```

## Commands / 微信命令

在微信中发送以下命令：

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/cwd <path>` | 设置你的工作目录 |
| `/pwd` | 查看当前工作目录 |
| `/clear` | 清除当前会话（开始新对话） |
| `/status` | 查看 bridge 运行状态 |
| `/model <name>` | 切换模型（opus/sonnet/haiku） |
| `/exec <cmd>` | 在工作目录执行 shell 命令 |
| 其他消息 | 转发给 Claude Code 处理 |

## Configuration / 配置

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `WCB_ALLOWED_USERS` | 用户白名单（逗号分隔） | 空 = 允许所有 |

### 数据文件

所有数据存储在 `~/.wechat-claude-bridge/`：

| 文件 | 内容 |
|------|------|
| `token.json` | iLink Bot 登录 token |
| `sessions.json` | 用户 → Claude session_id 映射 |
| `user_config.json` | 用户 → 工作目录、模型等配置 |
| `bridge.log` | 运行日志（4MB 轮转，保留 3 个） |
| `history/<user_id>.md` | 每个用户的消息历史 |

### 可调常量（bridge.py 顶部）

```python
RATE_LIMIT_S = 5      # 速率限制（秒）
MAX_MSG_LEN = 2000    # 微信单条消息最大字数
POLL_TIMEOUT_S = 38   # 长轮询超时
MAX_WORKERS = 5       # 并发 Claude 调用数
WEB_PORT = 9876       # Web 控制台端口
STREAM_INTERVAL = 3   # 流式输出推送间隔（秒）
```

## Architecture / 架构

```
bridge.py (~890 lines)
├── setup_logging()       日志设置（文件轮转 + stderr）
├── login()               iLink 扫码登录，获取 token
├── get_updates()         长轮询获取微信消息
├── extract_text()        从消息 item_list 提取文本
├── extract_image_url()   提取图片下载链接
├── handle_command()      内置命令分发（/help /cwd /clear 等）
├── ask_claude()          Claude CLI 调用 + 会话管理
│   └── run_claude_stream()  subprocess.Popen 流式读取
├── send_message()        通过 iLink API 回复微信
├── split_long_text()     长回复按段落拆分
├── run_web()             HTTP 状态接口（:9876）
└── main_loop()           主循环：收消息 → 线程池分发 → 回调发送
```

## iLink Bot API

基于腾讯微信 iLink Bot API (`ilinkai.weixin.qq.com`)：

- `GET  /ilink/bot/get_bot_qrcode` — 获取登录二维码
- `GET  /ilink/bot/get_qrcode_status` — 查询扫码状态
- `POST /ilink/bot/getupdates` — 长轮询收消息
- `POST /ilink/bot/sendmessage` — 发送消息

鉴权方式：`Authorization: Bearer <token>` + `AuthorizationType: ilink_bot_token`

## Web Console / Web 控制台

```bash
# 健康检查
curl http://127.0.0.1:9876/health

# 完整状态
curl http://127.0.0.1:9876/stats
```

返回 JSON：运行时间、总调用数、处理中请求数、活跃用户数、最近消息列表。

## License

MIT
