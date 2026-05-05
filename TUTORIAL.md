# WeChat-Claude-Bridge 上手指南

通过微信遥控 Claude Code 写代码、改文件、执行命令。本教程带你从零开始，5 分钟完成部署并发送第一条微信指令。

---

## 1. 工作原理

```
你 (微信) → iLink Bot API → bridge.py → Claude Code CLI → 你的项目目录
                                                        ↓
                                                (读写文件 / 执行命令)
                                                        ↓
你 (微信) ← iLink Bot API ← bridge.py ← Claude 的回复 ←┘
```

bridge.py 是一个中转程序，运行在你的服务器上。它一边通过微信 iLink API 收发消息，一边调用 Claude Code CLI 处理你的请求。

---

## 2. 准备工作

| 项目 | 要求 |
|------|------|
| 服务器 | Linux (Ubuntu 20.04+ / CentOS 7+)，24h 开机 |
| Python | 3.9+ |
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code` |
| 微信 | iOS 微信 8.0.70+，已开通 ClawBot 插件权限 |
| 网络 | 服务器可访问 `ilinkai.weixin.qq.com` |

> **如何开通 ClawBot 插件**: 微信 → 我 → 设置 → 插件 → 搜索"龙虾"或"ClawBot" → 开通。

---

## 3. 安装

```bash
# 1. 选择安装目录
mkdir -p ~/apps && cd ~/apps

# 2. 克隆项目
git clone https://github.com/LuYuxiaoPKU/Wechat-ClaudeCode-bridge.git
cd Wechat-ClaudeCode-bridge

# 3. 安装 Python 依赖
pip install -r requirements.txt

# 4. (可选) 添加到 PATH
echo 'export PATH="$PATH:'$(pwd)'"' >> ~/.bashrc
source ~/.bashrc
```

---

## 4. 首次运行 — 扫码登录

```bash
python3 bridge.py --login
```

**按顺序操作**：

1. 终端显示 GPLv3 条款 → 输入 `I_ACCEPT_GPLv3_AND_COMPLIANCE_TERMS` → 回车
2. 终端输出一个二维码 → **截图发到微信** → 在微信中长按识别
3. 微信弹出 ClawBot 确认 → 点击确认
4. 终端显示 `[OK] 登录成功！` → 开始长轮询

> 登录成功后 token 会保存到 `~/.wechat-claude-bridge/token.json`，下次直接运行 `python3 bridge.py` 即可。

---

## 5. 发送第一条消息

在微信中找到你的 ClawBot 联系人，发送：

```
hello，介绍一下你自己
```

你会看到：

1. `[THINK] Claude 正在思考...` — bridge 已收到消息
2. `[...]` — Claude 开始流式回复（增量推送）
3. 最终回复 — 经过 Markdown 转换的完整回答

> 由于 Claude Code 首次处理需要加载上下文，第一条消息可能需要 10-30 秒。后续对话会更快。

---

## 6. 核心命令速览

### 6.1 工作目录

Claude 默认在你的 bridge.py 启动目录下工作。你可以指定一个项目目录：

```
/cwd ~/my-project
/pwd
```

之后 Claude 的所有文件操作都在 `~/my-project` 下进行。

### 6.2 多会话管理

不同项目使用独立会话，互不干扰：

```
/new work              新建名为 work 的会话
/list                  查看所有会话，当前活跃的会话标记为 >>
/switch default        切回 default 会话
/reset work            重置 work 会话（切回 default）
```

> `/reset` 不会删除 Claude 的对话历史，可以随时用 `/attach <uuid>` 找回。

### 6.3 权限模式

```
/mode                   查看当前模式
/mode auto              Claude 自动批准常规操作（默认）
/mode ask               所有权限请求转发到微信，由你确认
```

在 `ask` 模式下，Claude 请求权限时会发送：

```
[PERM] Claude 请求权限:
Do you want to proceed? (y/n)
回复 yes（批准）或 no（拒绝）
（6 小时内有效，超时将自动取消）
```

回复 `yes` 批准，`no` 拒绝。6 小时内未回复自动取消。

### 6.4 系统监控

```
/cpu                    CPU 负载
/mem                    内存使用
/disk                   磁盘使用
/top                    进程 CPU Top20
/top mem                进程内存 Top20
```

### 6.5 执行命令

```
/exec ls -la
/exec git status
/exec df -h
```

> 命令执行超时 30 秒，输出限制 2000 字。

### 6.6 文件操作

```
/ls                    列出当前目录
/ls ~/my-project       列出指定目录
/send ~/data/chart.png          发送图片到微信
/send ~/data/report.pdf         发送文件到微信
```

### 6.7 定时提醒

```
/remind 30m 检查部署状态
/remind 9:00 每日站会
/remind 2h 开会
```

> 提醒到达后会通过 Claude 处理，可以写自然语言让 Claude 执行具体任务。

### 6.8 其他

```
/status                 查看 bridge 运行状态
/history                回看最近 3 轮对话
/history 5              回看最近 5 轮对话
/stop                   中断正在处理的请求
//任意指令              绕过桥接，直接发送给 Claude Code
```

---

## 7. 典型使用场景

### 场景 1: 远程修 Bug

```
# 你正在外面，同事说服务挂了

/cwd ~/production-api
查看 error.log 最后 50 行，分析原因并修复
```

Claude 会读日志、定位问题、修改代码、甚至重启服务。

### 场景 2: 生成周报

```
/cwd ~/work-notes
根据这周的 git log 生成周报，列出主要工作内容
```

### 场景 3: 数据分析

```
/cwd ~/data-project
分析 sales_2026.csv，找出销售额最高的前 10 个产品，画趋势图
```

Claude 会写 Python 脚本、执行、生成图表 → 用 `/send chart.png` 发到微信。

### 场景 4: 多项目并行

```
/new frontend
/cwd ~/vue-app
帮我修复登录页面的样式问题

/new backend
/cwd ~/go-api
查看 API 响应时间过长的原因

/switch frontend
上次的修复测试过了吗？
```

### 场景 5: 定时任务

```
/remind 9:00 查看 GitHub trending，总结今天的热门 AI 项目
/remind 17:00 检查今天所有项目的 git diff，汇总工作内容
```

---

## 8. 高级功能

### 8.1 HTTP Push API

外部系统（CI/CD、监控告警）可推送消息到微信：

```bash
curl -X POST http://127.0.0.1:9876/push \
  -H "Content-Type: application/json" \
  -d '{"user_id": "你的微信UID", "text": "部署完成，版本 v2.3.1"}'
```

### 8.2 用户白名单

```bash
WCB_ALLOWED_USERS="userA,userB" python3 bridge.py
```

只有白名单用户能使用 bridge，其他人收到 `[ERR] 你没有权限`。

### 8.3 系统看门狗

```
/watchdog start 5       每 5 分钟检查系统负载
/watchdog config cpu_percent 90    CPU 阈值设为 90%
/watchdog status         查看监控状态
/watchdog stop           停止监控
```

### 8.4 Docker 部署

```bash
docker-compose up -d
```

首次登录：
```bash
docker-compose run --rm bridge python3 bridge.py --login
# 扫码 → Ctrl+C → docker-compose up -d
```

---

## 9. 常见问题

### Q: 扫码后一直显示 "等待扫码"？

A: 二维码有 5 分钟有效期，过期会自动刷新（最多 3 次）。确认微信 ClawBot 插件已开通。

### Q: 消息发送失败，ret=None？

A: 检查 bridge.log 中的完整响应。通常是网络问题或 token 过期，尝试 `/login` 重新登录。

### Q: Claude 回复 "Session ID already in use"？

A: 上一个 Claude 进程未正常退出。bridge 已内置重试机制（等待 2 秒后重试），通常能自动恢复。

### Q: 如何 24 小时运行？

A: 使用 `nohup` 或 `systemd`：

```bash
# nohup
nohup python3 bridge.py > /dev/null 2>&1 &

# 或 screen/tmux
screen -S bridge
python3 bridge.py
# Ctrl+A D 断开
```

### Q: bridge.py 占多少内存？

A: 空闲约 50-80MB，处理请求时峰值约 200-500MB（取决于 Claude Code 的负载）。`/mem` 可随时查看。

### Q: 如何更新到最新版？

```bash
cd Wechat-ClaudeCode-bridge
git pull
# 重启 bridge
```

### Q: 怎么查看我的微信 UID？

A: 发送任意消息，bridge 日志会打印 `[MSG] [时间] 你的UID`。

---

## 10. 下一步

- 阅读 [README.md](./README.md) 查看完整命令参考和配置说明
- 查看 [GitHub](https://github.com/LuYuxiaoPKU/Wechat-ClaudeCode-bridge) 获取最新更新

---

> 有问题或建议？欢迎提 [Issue](https://github.com/LuYuxiaoPKU/Wechat-ClaudeCode-bridge/issues)
