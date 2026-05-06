# WeChat-Claude-Bridge

通过微信控制 [Claude Code](https://claude.ai/code) CLI，在任何地方用微信消息指挥 Claude 写代码、操作文件、执行命令。

Control Claude Code CLI via WeChat messages. Code, debug, and manage files from anywhere using WeChat.

> Built with **Claude Code**, powered by **DeepSeek V4 Pro[1m]**
>
> 新手入门请阅读 **[TUTORIAL.md / 上手指南](./TUTORIAL.md)**（5 分钟完成部署）

```
                        iLink API (HTTPS long polling)
  WeChat             <==================================>  bridge.py   <--->  Claude Code CLI
 (ClawBot)             sendmessage                   (~2400 lines)         (node)

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
                                   |   usage.json     |
                                   |   bridge.log     |
                                   |   history/*.md   |
                                   |   media/         |
                                   +------------------+
                                  ~/.wechat-claude-bridge/
```

## Prerequisites / 前提

- **操作系统**：Linux（推荐 Ubuntu 20.04+ / CentOS 7+）
- **微信 ClawBot 插件**（iOS 微信 8.0.70+，需开通权限）
- Python 3.9+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)
- （推荐）`psutil` 用于系统监控

## Quick Start / 快速开始

```bash
# 选择安装目录
mkdir -p /path/to/install && cd /path/to/install

git clone https://github.com/LuYuxiaoPKU/Wechat-ClaudeCode-bridge.git
cd Wechat-ClaudeCode-bridge
pip install -r requirements.txt

# 可选：添加到 PATH，之后在任何目录直接运行 bridge.py
export PATH="$PATH:$(pwd)"
echo 'export PATH="$PATH:'$(pwd)'"' >> ~/.bashrc

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
- **Markdown 转换** — 代码块/粗体/表格/链接自动转为微信可读纯文本
- **实时流式输出** — Claude 处理期间增量推送回复到微信，不等全部完成
- **并发处理** — ThreadPoolExecutor，多用户同时提问互不阻塞
- **多轮对话** — 基于 `--resume`/`--session-id` 的会话上下文保持
- **多会话管理** — `/new` `/list` `/switch` `/attach` `/reset` 命名会话，不同项目独立上下文
- **权限审批转发** — `/mode ask` 将 Claude 权限请求转发到微信确认（6 小时超时）
- **独立工作目录** — 每个用户可指定不同项目目录（`/cwd`）
- **命令逃逸** — `//cmd` 绕过桥接直接发送给 Claude Code CLI
- **系统监控** — `/cpu` `/mem` `/disk` `/top` 快速查询 + `/watchdog` 定时告警
- **定时提醒** — `/remind 30m` 或 `/remind 9:00` 自然语言定时
- **中断处理** — `/stop` 取消正在执行的 Claude 任务
- **文件发送** — `/send <path>` 发送图片/文件到微信
- **HTTP Push** — `POST :9876/push` 外部系统推送消息到微信
- **长消息拆分** — 超长回复按段落自动拆分
- **速率限制** — 5s 间隔防刷屏
- **用户白名单** — `WCB_ALLOWED_USERS` 环境变量
- **上线/下线通知** — 启动通知历史用户，退出通知在线用户
- **Web 控制台** — `http://127.0.0.1:9876` 查看状态
- **消息历史** — `~/.wechat-claude-bridge/history/<uid>.md` + `/history` 回看
- **日志轮转** — `~/.wechat-claude-bridge/bridge.log` (4MB ×3)

## Commands / 微信命令

```
[ 会话 & 模型 ]
  /new <name>              新建命名会话
  /list                    列出所有会话（显示当前）
  /switch <name>           切换活跃会话
  /attach <uuid> [name]   接入外部会话 UUID
  /reset [name]            重置会话（切回 default，default 不可清除）
  /mode <auto|ask>         权限模式

[ 工作目录 ]
  /cwd <path>              设置工作目录
  /pwd                     查看当前目录
  /ls [path]               列出目录内容

[ 系统 & 工具 ]
  /cpu                     查看 CPU 负载
  /mem                     查看内存使用
  /disk                    查看磁盘使用
  /top [cpu|mem]           查看进程 Top20
  /exec <shell cmd>        执行命令（30s 超时）
  /send <文件路径>         发送图片/文件到微信
  /stop                    中断正在处理的请求
  /status                  运行状态
  /watchdog <cmd>          系统监控（start/stop/status/config）
  /remind <时间> <消息>     定时提醒（支持 30m/2h/9:00）
  /history [N]             回看最近 N 轮对话
  /cleanup <target>        清理缓存（media/history/all）
  /login                   重新扫码登录

//<cmd>  绕过桥接，直接发送给 Claude Code CLI
```

## Configuration / 配置

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `WCB_ALLOWED_USERS` | 用户白名单（逗号分隔） | 空 = 允许所有 |

### 可调常量（bridge.py 顶部）

```python
RATE_LIMIT_S = 5          # 速率限制（秒）
MAX_MSG_LEN = 50000       # 微信单条消息最大字数
POLL_TIMEOUT_S = 38       # 长轮询超时
MAX_WORKERS = 5           # 并发 Claude 调用数
WEB_PORT = 9876           # Web 控制台端口
STREAM_INTERVAL = 3       # 流式输出轮询间隔（秒）
STREAM_MIN_CHARS = 100    # 流式输出最小推送字符数
PERMISSION_TIMEOUT_S = 21600  # 权限请求超时（6 小时）
```

### 数据文件（~/.wechat-claude-bridge/）

| 文件 | 内容 |
|------|------|
| `token.json` | iLink Bot 登录 token |
| `terms_accepted` | GPLv3 条款接受记录 |
| `sessions.json` | 用户多会话映射 |
| `user_config.json` | 用户配置（cwd/mode） |
| `watchdog.json` | 系统监控配置 |
| `reminders.json` | 定时提醒列表 |
| `bridge.log` | 运行日志（4MB 轮转） |
| `history/<uid>.md` | 消息历史 |
| `media/` | 下载的图片/文件 |

## Architecture / 架构

```
bridge.py (~2400 lines)
│
├── 启动 & 认证
│   ├── check_terms()          GPLv3 条款确认
│   ├── login()                iLink 扫码登录
│   └── /login                 运行时重新登录
│
├── 消息收发 (iLink API)
│   ├── get_updates()          长轮询收消息
│   ├── send_message()         发送文本消息
│   └── send_media()           发送图片/文件消息
│
├── 命令分发
│   ├── handle_command()       21 个内置命令
│   ├── /status /remind        内联命令
│   └── 模糊匹配               未知 /cmd 智能提示
│
├── Claude 集成
│   ├── ask_claude()           会话管理 + 权限检测
│   ├── run_claude_stream()   Popen 流式读取
│   └── on_stream 回调         增量推送至微信
│
├── Markdown 转换
│   ├── markdown_to_wechat()   代码块/粗体/表格/链接转换
│   └── _format_table()        管道表格式化
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
| `ilink/bot/sendmessage` | POST | 发送消息（文本/图片/文件） |

鉴权：`Authorization: Bearer <token>` + `AuthorizationType: ilink_bot_token`

## License

[GPLv3](LICENSE)
