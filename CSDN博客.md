# 我把 Claude Code 接入了微信，现在可以在任何地方指挥 AI 写代码了

> Built with Claude Code, powered by DeepSeek V4 Pro[1m]  
> 项目地址：https://github.com/LuYuxiaoPKU/Wechat-ClaudeCode-bridge

---

## 前言

作为一名开发者，我经常遇到这样的场景：人在外面，突然想到一个 bug 修复方案，或者需要紧急查看服务器状态，但手边没有电脑。

于是我花了几天时间，用 Python 写了一个桥接程序，把微信和 Claude Code CLI 连了起来。现在，我只需要在微信里发一条消息，Claude 就能帮我在服务器上写代码、查日志、监控系统。

这篇文章会详细介绍这个项目的设计思路、核心功能和技术实现。

---

## 效果展示

### 写代码

在微信中发送自然语言指令：

```
> 帮我在 bridge.py 中添加一个 /uptime 命令，
  返回系统运行时间
```

几十秒后，Claude 自动读取文件、编写代码、返回结果。

### 系统监控

```
/cpu
```
```
CPU   [███       ] 12%  load 0.5
```

```
/watchdog start 10
```
```
[OK] Watchdog 已启动
间隔: 10 分钟
CPU 阈值: 80%  内存阈值: 90%  磁盘阈值: 90%
```

CPU/内存/磁盘超阈值时，微信自动收到告警。

### 远程执行命令

```
/exec tail -50 /var/log/syslog
/ls /home/project/src
/top mem
```

完全等同于在服务器终端操作。

---

## 系统架构

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
```

整个系统分为四层：

1. **消息接入层** — 通过腾讯 iLink Bot API 长轮询接收微信消息
2. **命令分发层** — 16 个内置命令，模糊匹配智能提示
3. **AI 调用层** — ThreadPoolExecutor 并发调用 Claude Code CLI，流式输出
4. **运维监控层** — CPU/内存/磁盘阈值检测，微信告警

---

## 核心功能

### 1. 多会话管理

每个微信用户可以有多个独立命名的 Claude 会话：

```
/new debug      → 创建调试专用会话
/new frontend   → 创建前端项目会话
/list           → 查看所有会话
/switch debug   → 切换到调试会话
```

不同项目之间完全隔离，上下文不会混淆。

### 2. 权限审批转发

这是最实用的功能之一。Claude Code 在执行危险操作前需要确认，但我不在电脑前怎么办？

```
/mode ask       → 切换到询问模式
```

当 Claude 需要权限时，审批请求会转发到微信：

```
[PERM] Claude 请求权限:
Claude needs your permission to run: rm -rf /tmp/cache

回复 yes/no
```

在微信中回复 `yes`，Claude 继续执行；回复 `no`，拒绝操作。

### 3. 命令逃逸

bridge 有自己的 `/model` 命令，但 Claude Code CLI 也有 `/model` 命令。用 `//` 前缀逃逸：

```
/model sonnet    → bridge 拦截，切换 bridge 配置
//model sonnet   → 转发给 Claude Code CLI 处理
```

### 4. 智能模糊匹配

不小心打错命令？

```
/top1
→ [?] /top1 不是有效命令
  试试: /top
  输入 /help 查看全部
```

不会浪费 token 转发给 Claude 处理。

### 5. 系统监控 (Watchdog)

```
/watchdog start 10         → 每 10 分钟检查一次
/watchdog config cpu_percent 80   → CPU 超过 80% 告警
/watchdog config disk_percent 90  → 磁盘超过 90% 告警
/watchdog paths /,/data           → 监控这些磁盘
```

持久化配置，重启后继续运行。告警冷却 30 分钟，不会刷屏。

### 6. HTTP Push API

```bash
# GitHub Actions 部署完成后通知你
curl -X POST http://server:9876/push \
  -H "Content-Type: application/json" \
  -d '{"user_id": "xxx", "text": "部署成功！"}'
```

### 7. 定时提醒

```
/remind 30m 检查部署状态
/remind 9:00 每日站会
/remind 2h 开会
```

---

## 技术细节

### Claude Code CLI 集成

```python
def run_claude_stream(text, cwd=None, model=None, extra_args=None):
    """Popen 流式执行 Claude CLI，实时推送增量文本"""
    proc = subprocess.Popen(
        ["claude", "-p", "--output-format", "text",
         "--permission-mode", "auto"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=cwd,
        env={"LANG": "en_US.UTF-8", ...},
    )
    # 写入 stdin，读取 stdout，每 3 秒推送增量
    ...
```

使用 `subprocess.Popen` 而非 `subprocess.run`，实现了真正的流式输出——Claude 思考期间，bridge 每 3 秒推送一段增量文本到微信。

### 跨平台系统监控

```python
try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

def collect_metrics():
    if _HAS_PSUTIL:
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        disks = psutil.disk_partitions()
    else:
        # Linux: /proc/meminfo, /proc/mounts
        # macOS: sysctl, vm_stat, mount
        # Windows: drive letter enumeration
        ...
```

优先使用 psutil，回退到各平台原生接口，真正做到跨平台。

### iLink Bot API

腾讯 2026 年 3 月通过 OpenClaw 平台开放了微信个人账号的 Bot API，底层协议名为 iLink。这是微信**首次官方合法开放**的个人 Bot 接口。

关键端点：
- `GET /ilink/bot/get_bot_qrcode` — 获取登录二维码
- `POST /ilink/bot/getupdates` — 长轮询收消息（38 秒超时）
- `POST /ilink/bot/sendmessage` — 发送消息
- `POST /ilink/bot/sendtyping` — "正在输入"状态

---

## 命令速查表

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/cwd <path>` | 设置工作目录 |
| `/pwd` | 查看工作目录 |
| `/new <name>` | 新建命名会话 |
| `/list` | 列出所有会话 |
| `/switch <name>` | 切换会话 |
| `/clear` | 清除当前会话 |
| `/model <o\|s\|h>` | 切换模型 |
| `/mode <auto\|ask>` | 权限模式 |
| `/cpu` | CPU 负载 |
| `/mem` | 内存使用 |
| `/disk` | 磁盘使用 |
| `/top [cpu\|mem]` | 进程 Top20 |
| `/ls [path]` | 列出目录 |
| `/exec <cmd>` | 执行命令 |
| `/status` | 运行状态 |
| `/watchdog` | 系统监控 |
| `/remind` | 定时提醒 |
| `/cleanup` | 清理缓存 |
| `/login` | 重新登录 |
| `//<cmd>` | 逃逸到 Claude |

---

## 快速开始

```bash
git clone https://github.com/LuYuxiaoPKU/Wechat-ClaudeCode-bridge.git
cd Wechat-ClaudeCode-bridge
pip install -r requirements.txt
python3 bridge.py --login
```

前提：iOS 微信 8.0.70+，已开通 ClawBot 插件权限。

---

## 写在最后

这个项目的核心思路是：**让 Claude Code 成为你的随身开发助手**。不需要打开电脑，不需要 SSH 登录，微信发一条消息就能指挥 AI 干活。

项目采用 GPLv3 开源协议，欢迎 Star、PR 和 Issue。

如果你也在用微信 ClawBot，不妨试试这个桥接方案，让你的 Claude Code 真正 7x24 在线。

> GitHub: https://github.com/LuYuxiaoPKU/Wechat-ClaudeCode-bridge
