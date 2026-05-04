# WeChat-Claude-Bridge

通过微信控制 [Claude Code](https://claude.ai/code) CLI，在任何地方用微信消息指挥 Claude 写代码、操作文件、执行命令。

Control Claude Code CLI via WeChat messages. Code, debug, and manage files from anywhere using WeChat.

> Built with **Claude Code**, powered by **DeepSeek V4 Pro[1m]**

```
                        iLink API (HTTPS long polling)
  WeChat             <==================================>  bridge.py   <--->  Claude Code CLI
 (ClawBot)             sendmessage / sendtyping          (~1800 lines)         (node)

                                                             |
  External Systems      HTTP POST            .---------------+---------------.
 (GitHub Actions, CI) <--------------------> |               |               |
                         /push               v               v               v
                                        ThreadPool        Web :9876      Watchdog
                                          (x5)        /health /stats    Remind
                                             |         /push
                                             v
                                   +------------------+
                                   |   Data & State   |
                                   |   token.json     |
                                   |   sessions.json  |
                                   |   user_config    |
                                   |   watchdog.json  |
                                   |   reminders.json |
                                   |   bridge.log     |
                                   |   history/*.md   |
                                   |   media/         |
                                   +------------------+
                                  ~/.wechat-claude-bridge/
```

## Prerequisites / 前提

- **微信 ClawBot 插件**（iOS 微信 8.0.70+，需开通权限）
- Python 3.9+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)
- （可选）`psutil` 用于跨平台系统监控

## Quick Start / 快速开始

```bash
git clone https://github.com/LuYuxiaoPKU/Wechat-ClaudeCode-bridge.git
cd Wechat-ClaudeCode-bridge
pip install -r requirements.txt

# 首次运行 — 接受 GPLv3 条款 → 扫码登录
python3 bridge.py --login

# 后续运行 — 自动复用 token
python3 bridge.py
```

### Docker

```bash
docker-compose up -d
```

## Features / 功能

- **微信 ↔ Claude Code 双向桥接** — 消息转发 + Claude 回复返回微信
- **并发处理** — ThreadPoolExecutor，多用户同时提问互不阻塞
- **流式输出** — Claude 思考期间持续推送增量文本
- **多轮对话** — 基于 `--resume`/`--session-id` 的会话上下文保持
- **多会话管理** — `/new` `/list` `/switch` 命名会话，不同项目独立上下文
- **权限审批转发** — `/mode ask` 将 Claude 权限请求转发到微信确认
- **独立工作目录** — 每个用户可指定不同项目目录（`/cwd`）
- **模型切换** — 微信内切换 `opus`/`sonnet`/`haiku`（`/model`）
- **命令逃逸** — `//cmd` 绕过桥接直接发送给 Claude Code CLI
- **系统监控** — `/cpu` `/mem` `/disk` 快速查询 + `/watchdog` 定时告警
- **定时提醒** — `/remind 30m` 或 `/remind 9:00` 自然语言定时
- **HTTP Push** — `POST :9876/push` 外部系统推送消息到微信
- **长消息拆分** — 超长回复按段落自动拆分
- **速率限制** — 5s 间隔防刷屏
- **用户白名单** — `WCB_ALLOWED_USERS` 环境变量
- **上线/下线通知** — 启动通知历史用户，退出通知在线用户
- **Web 控制台** — `http://127.0.0.1:9876` 查看状态
- **消息历史** — `~/.wechat-claude-bridge/history/<uid>.md`
- **日志轮转** — `~/.wechat-claude-bridge/bridge.log` (4MB ×3)

## Commands / 微信命令

```
[ 会话 & 模型 ]
  /new <name>              新建命名会话
  /list                    列出所有会话
  /switch <name>           切换活跃会话
  /clear                   清除当前会话
  /model <opus|sonnet|haiku> 切换模型
  /mode <auto|ask>         权限模式

[ 工作目录 ]
  /cwd <path>              设置工作目录
  /pwd                     查看当前目录

[ 系统 & 工具 ]
  /cpu                     查看 CPU 负载
  /mem                     查看内存使用
  /disk                    查看磁盘使用
  /exec <shell cmd>        执行命令
  /status                  运行状态
  /watchdog <cmd>          系统监控
  /remind <时间> <消息>     定时提醒
  /cleanup <target>        清理缓存

//<cmd>  绕过桥接，直接发送给 Claude Code CLI
```

## Configuration / 配置

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `WCB_ALLOWED_USERS` | 用户白名单（逗号分隔） | 空 = 允许所有 |

### 可调常量（bridge.py 顶部）

```python
RATE_LIMIT_S = 5      # 速率限制（秒）
MAX_MSG_LEN = 2000    # 微信单条消息最大字数
POLL_TIMEOUT_S = 38   # 长轮询超时
MAX_WORKERS = 5       # 并发 Claude 调用数
WEB_PORT = 9876       # Web 控制台端口
```

### 数据文件（~/.wechat-claude-bridge/）

| 文件 | 内容 |
|------|------|
| `token.json` | iLink Bot 登录 token |
| `terms_accepted` | GPLv3 条款接受记录 |
| `sessions.json` | 用户多会话映射 |
| `user_config.json` | 用户配置（cwd/model/mode） |
| `watchdog.json` | 系统监控配置 |
| `reminders.json` | 定时提醒列表 |
| `bridge.log` | 运行日志（4MB 轮转） |
| `history/<uid>.md` | 消息历史 |
| `media/` | 下载的图片/文件 |

## Architecture / 架构

```
bridge.py (~1800 lines)
│
├── 启动 & 认证
│   ├── check_terms()          GPLv3 条款确认
│   ├── login()                iLink 扫码登录
│   └── /login                 运行时重新登录
│
├── 消息收发 (iLink API)
│   ├── get_updates()          长轮询收消息
│   ├── send_message()         发送消息
│   └── send_typing()          "正在输入" 状态
│
├── 命令分发
│   ├── handle_command()       16 个内置命令
│   ├── /status /remind        内联命令
│   └── 模糊匹配               未知 /cmd 智能提示
│
├── Claude 集成
│   ├── ask_claude()           会话管理 + 权限检测
│   └── run_claude_stream()   Popen 流式读取
│
├── 系统监控
│   ├── collect_metrics()      跨平台指标 (psutil/native)
│   ├── check_watchdog()       阈值检测 + 微信告警
│   └── /cpu /mem /disk /top  快速查询
│
├── 辅助功能
│   ├── split_long_text()      长消息拆分
│   ├── _progress_bar()        Unicode 进度条
│   └── extract_media_url()    图片/文件提取
│
├── Web 服务
│   └── WebHandler             :9876 (/health /stats /push)
│
└── 后台线程
    ├── ThreadPoolExecutor     并发 Claude 调用 (x5)
    ├── reminder_thread_fn     定时提醒检查
    └── watchdog_thread_fn     系统监控轮询
```

## Web Console / Web API

```bash
# 健康检查
curl http://127.0.0.1:9876/health

# 运行统计
curl http://127.0.0.1:9876/stats

# 外部推送
curl -X POST http://127.0.0.1:9876/push \
  -H "Content-Type: application/json" \
  -d '{"user_id": "xxx", "text": "部署完成"}'
```

## iLink Bot API

基于腾讯微信 iLink Bot API (`ilinkai.weixin.qq.com`)：

| 端点 | 方法 | 用途 |
|------|------|------|
| `ilink/bot/get_bot_qrcode` | GET | 获取登录二维码 |
| `ilink/bot/get_qrcode_status` | GET | 查询扫码状态 |
| `ilink/bot/getupdates` | POST | 长轮询收消息 |
| `ilink/bot/sendmessage` | POST | 发送消息 |
| `ilink/bot/sendtyping` | POST | "正在输入"状态 |

鉴权：`Authorization: Bearer <token>` + `AuthorizationType: ilink_bot_token`

## License

[GPLv3](LICENSE)
