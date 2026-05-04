#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wechat-claude-bridge — 微信 ClawBot ↔ Claude Code 桥接
基于腾讯 iLink Bot API，将微信消息转发给 Claude Code Agent 处理。

用法:
  python3 bridge.py              # 默认运行（自动复用 token）
  python3 bridge.py --login      # 强制重新扫码登录
"""

import os
import sys
import json
import time
import uuid
import signal
import random
import base64
import shutil
import logging
import logging.handlers
import tempfile
import threading
import subprocess
import concurrent.futures
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import qrcode

# ==========================================================================
#  Configuration
# ==========================================================================

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
CHANNEL_VERSION = "1.0.2"
DATA_DIR = Path.home() / ".wechat-claude-bridge"
TOKEN_FILE = DATA_DIR / "token.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
USER_CONFIG_FILE = DATA_DIR / "user_config.json"
LOG_FILE = DATA_DIR / "bridge.log"
POLL_TIMEOUT_S = 38
RATE_LIMIT_S = 5
MAX_MSG_LEN = 2000
MAX_WORKERS = 5
WEB_PORT = 9876
STREAM_INTERVAL = 3       # 流式输出间隔（秒）
STREAM_MIN_CHARS = 100    # 流式输出最小字符增量

# 用户白名单 — 空集合 = 允许所有用户
# 也可通过环境变量设置: WCB_ALLOWED_USERS=uid1,uid2
_ALLOWED_ENV = os.environ.get("WCB_ALLOWED_USERS", "")
ALLOWED_USERS = set(u.strip() for u in _ALLOWED_ENV.split(",") if u.strip())

# ==========================================================================
#  Logging
# ==========================================================================


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # 同时输出到 stderr
    root.addHandler(logging.StreamHandler(sys.stderr))


log = logging.getLogger(__name__)

# ==========================================================================
#  Helpers
# ==========================================================================


def random_wechat_uin():
    rand_uint32 = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(rand_uint32).encode()).decode()


def build_headers(token=None, body=None):
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": random_wechat_uin(),
    }
    if body is not None:
        headers["Content-Length"] = str(len(json.dumps(body).encode("utf-8")))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def user_session_uuid(user_id):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wechat-claude-bridge:{user_id}"))


# ==========================================================================
#  iLink API 客户端
# ==========================================================================


def api_get(base_url, path):
    url = f"{base_url.rstrip('/')}/{path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_post(base_url, endpoint, body, token, timeout_ms=15000):
    url = f"{base_url.rstrip('/')}/{endpoint}"
    payload = {**body, "base_info": {"channel_version": CHANNEL_VERSION}}
    headers = build_headers(token, payload)
    try:
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=timeout_ms / 1000)
        resp.raise_for_status()
        return resp.json()
    except requests.Timeout:
        return None
    except requests.RequestException as e:
        raise RuntimeError(f"API POST {endpoint} 失败: {e}")


# ==========================================================================
#  Session / 登录
# ==========================================================================


def load_session():
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return None


def save_session(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    TOKEN_FILE.chmod(0o600)


def clear_session():
    try:
        TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass


def load_user_sessions():
    if SESSIONS_FILE.exists():
        return json.loads(SESSIONS_FILE.read_text())
    return {}


def save_user_sessions(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_text(json.dumps(data, indent=2))


def load_user_config():
    if USER_CONFIG_FILE.exists():
        return json.loads(USER_CONFIG_FILE.read_text())
    return {}


def save_user_config(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_FILE.write_text(json.dumps(data, indent=2))


def render_qr_terminal(url):
    qr = qrcode.QRCode()
    qr.add_data(url)
    qr.make()
    qr.print_ascii()


def login(on_qr=None, on_status=None):
    logger = on_status or print
    logger("[LOGIN] 开始微信扫码登录...")

    qr_resp = api_get(DEFAULT_BASE_URL, f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}")
    current_qrcode = qr_resp["qrcode"]

    logger("[QR] 请用微信扫描以下二维码：")
    if on_qr:
        on_qr(qr_resp["qrcode_img_content"])
    else:
        render_qr_terminal(qr_resp["qrcode_img_content"])

    logger("[WAIT] 等待扫码...")
    deadline = time.time() + 5 * 60
    refresh_count = 0

    while time.time() < deadline:
        status = api_get(
            DEFAULT_BASE_URL,
            f"ilink/bot/get_qrcode_status?qrcode={current_qrcode}",
        )
        s = status["status"]
        if s == "wait":
            sys.stdout.write(".")
            sys.stdout.flush()
        elif s == "scaned":
            logger("[SCAN] 已扫码，请在微信端确认...")
        elif s == "expired":
            refresh_count += 1
            if refresh_count > 3:
                raise RuntimeError("二维码多次过期，请重新运行")
            logger(f"[WAIT] 二维码过期，刷新中 ({refresh_count}/3)...")
            new_qr = api_get(DEFAULT_BASE_URL, f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}")
            current_qrcode = new_qr["qrcode"]
            if on_qr:
                on_qr(new_qr["qrcode_img_content"])
            else:
                render_qr_terminal(new_qr["qrcode_img_content"])
        elif s == "confirmed":
            logger("[OK] 登录成功！")
            session = {
                "token": status["bot_token"],
                "baseUrl": status.get("baseurl", DEFAULT_BASE_URL),
                "accountId": status["ilink_bot_id"],
                "userId": status["ilink_user_id"],
                "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            }
            save_session(session)
            logger(f"Bot ID: {session['accountId']}")
            return session
        time.sleep(1)

    raise RuntimeError("登录超时")


# ==========================================================================
#  消息收发
# ==========================================================================


def get_updates(base_url, token, buf=""):
    resp = api_post(
        base_url, "ilink/bot/getupdates",
        {"get_updates_buf": buf}, token,
        timeout_ms=POLL_TIMEOUT_S * 1000 + 5000,
    )
    if resp is None:
        return {"ret": 0, "msgs": [], "get_updates_buf": buf}
    return resp


def send_message(base_url, token, to_user_id, text, context_token=""):
    client_id = f"wcb-{uuid.uuid4()}"
    api_post(
        base_url, "ilink/bot/sendmessage",
        {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            }
        },
        token,
    )
    return client_id


def extract_text(msg):
    for item in msg.get("item_list", []):
        t = item.get("type")
        if t == 1 and item.get("text_item", {}).get("text"):
            return item["text_item"]["text"]
        if t == 3 and item.get("voice_item", {}).get("text"):
            return f"[语音] {item['voice_item']['text']}"
        if t == 2:
            return "[图片]"
        if t == 4:
            fn = item.get("file_item", {}).get("file_name", "")
            return f"[文件] {fn}"
        if t == 5:
            return "[视频]"
    return "[空消息]"


def extract_image_url(msg):
    """从图片消息中提取下载链接，返回 (url, filename) 或 (None, None)"""
    for item in msg.get("item_list", []):
        if item.get("type") != 2:
            continue
        img = item.get("image_item", {})
        for key in ("url", "img_url", "file_url", "cdn_url", "download_url"):
            u = img.get(key)
            if u:
                return u, img.get("file_name", f"image_{uuid.uuid4().hex[:8]}.jpg")
    return None, None


def download_file(url, save_path):
    """下载文件到本地，返回是否成功"""
    try:
        r = requests.get(url, timeout=30, stream=True)
        r.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return True
    except Exception as e:
        log.warning(f"下载文件失败: {url} — {e}")
        return False


# ==========================================================================
#  Claude Code CLI 集成
# ==========================================================================


def run_claude_stream(text, cwd=None, model=None, extra_args=None, timeout_s=300):
    """
    使用 subprocess.Popen 执行 claude CLI，流式返回增量文本。

    Yields: (is_partial: bool, chunk: str)
      - is_partial=True:  流式增量（尚未完成）
      - is_partial=False: 最终完整结果文本
    """
    cmd = ["claude", "-p", "--output-format", "text", "--permission-mode", "auto"]
    if model:
        cmd.extend(["--model", model])
    if extra_args:
        cmd.extend(extra_args)

    env = {**os.environ, "CLAUDE_CODE_SIMPLE": "1",
           "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError:
        yield (False, "[ERR] 找不到 claude 命令，请确认 Claude Code 已安装")
        return

    # 写入 stdin（避免死锁，需在单独线程）
    def _write_stdin():
        try:
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        except Exception:
            pass

    threading.Thread(target=_write_stdin, daemon=True).start()

    # 读取 stdout 到线程安全缓冲区
    output_chunks = []
    stderr_chunks = []
    read_done = threading.Event()

    def _read_output():
        for line in iter(proc.stdout.readline, b""):
            output_chunks.append(line.decode("utf-8"))
        read_done.set()

    def _read_stderr():
        for line in iter(proc.stderr.readline, b""):
            stderr_chunks.append(line.decode("utf-8"))

    reader = threading.Thread(target=_read_output, daemon=True)
    reader.start()
    threading.Thread(target=_read_stderr, daemon=True).start()

    # 轮询，每隔 STREAM_INTERVAL 秒推送增量
    start = time.time()
    sent_pos = 0

    while not read_done.is_set() and proc.poll() is None:
        elapsed = time.time() - start
        if elapsed > timeout_s:
            proc.kill()
            yield (True, "\n[ERR] 处理超时，请简化您的问题")
            return

        # 收集当前所有输出
        current = "".join(output_chunks)
        new_text = current[sent_pos:]
        if len(new_text) >= STREAM_MIN_CHARS:
            yield (True, new_text)
            sent_pos = len(current)

        time.sleep(STREAM_INTERVAL)

    reader.join(timeout=5)
    proc.wait(timeout=5)

    # 推送剩余增量
    current = "".join(output_chunks)
    remaining = current[sent_pos:]
    if remaining.strip():
        yield (True, remaining)

    stderr = "".join(stderr_chunks).strip()
    if proc.returncode != 0:
        err_msg = stderr or "(无错误输出)"
        yield (True, f"\n[ERR] {err_msg}")
    else:
        yield (False, current.strip())


def ask_claude(text, user_id, sessions, cwd=None, model=None,
               on_stream=None):
    """
    调用 claude CLI 处理消息。

    on_stream(chunk, is_partial): 流式回调（在线程中调用）
    返回最终完整文本，或错误信息。
    """
    session_id = user_session_uuid(user_id)
    has_session = user_id in sessions

    def _call_with_stream(extra_args):
        acc = ""
        for is_partial, chunk in run_claude_stream(
            text, cwd=cwd, model=model, extra_args=extra_args
        ):
            if on_stream:
                on_stream(chunk, is_partial)
            if is_partial:
                acc += chunk
            else:
                acc = chunk
        return acc.strip()

    # 首次尝试：resume 已有会话
    if has_session:
        output = _call_with_stream(["--resume", session_id])
        if output and not output.startswith("[ERR]"):
            return output or "（Claude 无回复）"

        # resume 失败 — 降级
        sessions.pop(user_id, None)
        save_user_sessions(sessions)
        has_session = False

    # 新会话
    output = _call_with_stream(["--session-id", session_id])
    if output.startswith("[ERR]"):
        return output

    if not has_session:
        sessions[user_id] = session_id
        save_user_sessions(sessions)

    return output or "（Claude 无回复）"


# ==========================================================================
#  命令处理
# ==========================================================================


def handle_command(stripped, from_user, user_config, sessions,
                   base_url, token, ctx):
    """
    处理内置命令。返回 (handled, reply_text)。
    handled=False 表示应转发给 Claude。
    """
    # ---- /help ----
    if stripped == "/help":
        return True, (
            "[WeChat-Claude-Bridge]\n"
            "/help                     — 显示此帮助\n"
            "/cwd <path> 或 /dir <path> — 设置工作目录\n"
            "/cwd 或 /pwd              — 查看当前工作目录\n"
            "/clear                    — 清除当前会话\n"
            "/status                   — 查看 bridge 运行状态\n"
            "/model <name>             — 切换模型 (opus/sonnet/haiku)\n"
            "/exec <shell命令>          — 在工作目录执行命令\n"
        )

    # ---- /cwd /dir /pwd ----
    if stripped.startswith("/cwd ") or stripped.startswith("/dir "):
        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            p = Path(parts[1].strip()).expanduser().resolve()
            if p.is_dir():
                cfg = user_config.setdefault(from_user, {})
                cfg["cwd"] = str(p)
                save_user_config(user_config)
                return True, f"[OK] 工作目录已设置为: {p}"
            else:
                return True, f"[ERR] 目录不存在: {p}"
        return True, "[USAGE] /cwd <path> 或 /dir <path>"

    if stripped in ("/cwd", "/dir", "/pwd"):
        cfg = user_config.get(from_user, {})
        cwd = cfg.get("cwd", "(未设置，使用 bridge 进程目录)")
        return True, f"[CWD] {cwd}"

    # ---- /clear ----
    if stripped == "/clear":
        sessions.pop(from_user, None)
        save_user_sessions(sessions)
        return True, "[OK] 会话已清除，下一条消息将开始新对话"

    # ---- /model ----
    if stripped.startswith("/model "):
        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            m = parts[1].strip().lower()
            if m in ("opus", "sonnet", "haiku"):
                cfg = user_config.setdefault(from_user, {})
                cfg["model"] = m
                save_user_config(user_config)
                # 切换模型时清除旧会话（不同模型不兼容）
                sessions.pop(from_user, None)
                save_user_sessions(sessions)
                return True, f"[OK] 模型已切换为 {m}，会话已重置"
            else:
                return True, "[USAGE] /model <opus|sonnet|haiku>"

    if stripped == "/model":
        cfg = user_config.get(from_user, {})
        m = cfg.get("model", "默认")
        return True, f"[MODEL] 当前模型: {m}"

    # ---- /exec ----
    if stripped.startswith("/exec "):
        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            shell_cmd = parts[1].strip()
            cfg = user_config.get(from_user, {})
            exec_cwd = cfg.get("cwd") or os.getcwd()
            try:
                result = subprocess.run(
                    shell_cmd, shell=True, capture_output=True,
                    encoding="utf-8", timeout=30, cwd=exec_cwd,
                    env={"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
                         "PATH": os.environ.get("PATH", "/usr/bin"),
                         "HOME": os.environ.get("HOME", "/root")},
                )
                out = result.stdout.strip() or "(无输出)"
                if result.returncode != 0:
                    err = result.stderr.strip()
                    if err:
                        out += f"\n[STDERR] {err}"
                    return True, f"[EXIT {result.returncode}] {out[:1800]}"
                return True, out[:2000]
            except subprocess.TimeoutExpired:
                return True, "[ERR] 命令执行超时（30s）"
            except Exception as e:
                return True, f"[ERR] 命令执行失败: {e}"
        return True, "[USAGE] /exec <shell命令>"

    return False, ""


# ==========================================================================
#  消息拆分
# ==========================================================================


def split_long_text(text, max_len=MAX_MSG_LEN):
    if len(text) <= max_len:
        return [text]
    chunks = []
    for paragraph in text.split("\n\n"):
        if len(paragraph) <= max_len:
            chunks.append(paragraph)
        else:
            buf = ""
            for line in paragraph.split("\n"):
                if len(buf) + len(line) + 1 <= max_len:
                    buf = (buf + "\n" + line) if buf else line
                else:
                    if buf:
                        chunks.append(buf)
                    while len(line) > max_len:
                        chunks.append(line[:max_len])
                        line = line[max_len:]
                    buf = line if line else ""
            if buf:
                chunks.append(buf)
    merged = []
    for c in chunks:
        if merged and len(merged[-1]) + len(c) + 2 <= max_len:
            merged[-1] = merged[-1] + "\n\n" + c
        else:
            merged.append(c)
    return merged


# ==========================================================================
#  Web 控制台
# ==========================================================================


class WebHandler(BaseHTTPRequestHandler):
    stats_ref = None  # 由 run_web 注入

    def log_message(self, *args):
        pass  # 静默 HTTP 日志

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"status": "ok"})
            return

        if self.path in ("/", "/stats"):
            s = self.stats_ref or {}
            uptime = time.time() - s.get("start_time", time.time())
            h, m_ = divmod(int(uptime), 3600)
            m, sec = divmod(m_, 60)
            self._send_json({
                "uptime": f"{h}h {m}m {sec}s",
                "total_calls": s.get("total_calls", 0),
                "in_flight": len(s.get("in_flight", set())),
                "active_users": len(s.get("users_seen", set())),
                "recent_messages": s.get("recent", [])[-20:],
            })
            return

        self._send_json({"error": "not found"}, 404)


def run_web(stats):
    handler = type("_H", (WebHandler,), {"stats_ref": stats})
    try:
        server = HTTPServer(("127.0.0.1", WEB_PORT), handler)
        server.serve_forever()
    except Exception as e:
        log.warning(f"Web 控制台启动失败: {e}")


# ==========================================================================
#  主循环
# ==========================================================================


def main_loop(session, sessions, user_config):
    token = session["token"]
    base_url = session["baseUrl"]
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)

    # 统计信息（线程安全，仅主线程写入）
    stats = {
        "start_time": time.time(),
        "total_calls": 0,
        "in_flight": set(),
        "users_seen": set(),
        "recent": [],  # [(ts, user_id, request[:60], reply_preview[:80]), ...]
    }

    # 启动 Web 控制台
    web_thread = threading.Thread(target=run_web, args=(stats,), daemon=True)
    web_thread.start()

    running = True

    def on_sigint(sig, frame):
        nonlocal running
        print("\n\n[BYE] 正在退出...")
        running = False

    signal.signal(signal.SIGINT, on_sigint)

    print(f"[START] 开始长轮询收消息（Ctrl+C 退出）")
    print(f"[START] Web 控制台: http://127.0.0.1:{WEB_PORT}\n")
    buf = ""
    contacted_users = set()
    last_request = {}
    in_flight = set()  # 正在处理中的 user_id

    # 消息历史记录目录
    history_dir = DATA_DIR / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    def _save_history(uid, role, content):
        """追加一行到用户的历史文件"""
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            line = f"## {ts} [{role}]\n{content}\n\n"
            (history_dir / f"{uid}.md").open("a", encoding="utf-8").write(line)
        except Exception:
            pass

    def _on_claude_done(future, from_user, ctx, start_time):
        """Claude 调用完成后的回调（在线程池工作线程中执行）"""
        try:
            reply = future.result()
        except Exception as e:
            reply = f"[ERR] {e}"

        # 发送回复
        chunks = split_long_text(reply)
        for chunk in chunks:
            try:
                send_message(base_url, token, from_user, chunk, ctx)
            except Exception as e:
                log.warning(f"发送回复给 {from_user} 失败: {e}")

        elapsed = time.time() - start_time
        preview = reply[:80].replace("\n", " ")
        stats["total_calls"] += 1
        stats["recent"].append((
            time.strftime("%H:%M:%S"), from_user,
            preview,
            f"{elapsed:.1f}s{' [' + str(len(chunks)) + '条]' if len(chunks) > 1 else ''}"
        ))
        if len(stats["recent"]) > 200:
            stats["recent"] = stats["recent"][-200:]

        in_flight.discard(from_user)
        stats["in_flight"] = in_flight

        # 记录历史
        _save_history(from_user, "Claude", reply)

        print(f"   [OK] [{elapsed:.1f}s] {preview}{'...' if len(reply) > 80 else ''}"
              f"{' [' + str(len(chunks)) + '条]' if len(chunks) > 1 else ''}")

    while running:
        try:
            resp = get_updates(base_url, token, buf)
            if resp.get("get_updates_buf"):
                buf = resp["get_updates_buf"]

            for msg in resp.get("msgs", []):
                if msg.get("message_type") != 1:
                    # 记录非文本消息用于调试
                    log.debug(f"非文本消息: {json.dumps(msg, ensure_ascii=False)[:500]}")
                    continue

                from_user = msg["from_user_id"]
                text = extract_text(msg)
                ctx = msg.get("context_token", "")
                contacted_users.add(from_user)
                stats["users_seen"].add(from_user)

                ts = time.strftime("%H:%M:%S")
                print(f"[MSG] [{ts}] {from_user}")
                print(f"   {text}")

                # ---- 白名单检查 ----
                if ALLOWED_USERS and from_user not in ALLOWED_USERS:
                    send_message(base_url, token, from_user,
                                 "[ERR] 你没有权限使用此 Bot", ctx)
                    log.info(f"拒绝未授权用户: {from_user}")
                    continue

                # ---- 内置命令 ----
                stripped = text.strip()
                handled, reply = handle_command(
                    stripped, from_user, user_config, sessions,
                    base_url, token, ctx
                )
                if handled:
                    send_message(base_url, token, from_user, reply, ctx)
                    print(f"   [CMD] {reply[:80]}")
                    continue

                # ---- /status（需要 stats 上下文） ----
                if stripped == "/status":
                    uptime_s = time.time() - stats["start_time"]
                    h, m_ = divmod(int(uptime_s), 3600)
                    m, s = divmod(m_, 60)
                    send_message(base_url, token, from_user,
                                 f"[STATUS]\n"
                                 f"运行时间: {h}h {m}m {s}s\n"
                                 f"总调用数: {stats['total_calls']}\n"
                                 f"处理中:   {len(in_flight)}\n"
                                 f"历史用户: {len(stats['users_seen'])}", ctx)
                    continue

                # ---- 速率限制 ----
                now_ts = time.time()
                if from_user in last_request and \
                   (now_ts - last_request[from_user]) < RATE_LIMIT_S:
                    send_message(base_url, token, from_user,
                                 f"[WAIT] 请等待 {RATE_LIMIT_S}s 后再发送消息", ctx)
                    continue

                # ---- 用户已有请求在处理中 ----
                if from_user in in_flight:
                    send_message(base_url, token, from_user,
                                 "[WAIT] 上一条消息仍在处理中，请等待", ctx)
                    continue

                # ---- 转发 Claude ----
                # 立即回复 "正在思考"
                send_message(base_url, token, from_user,
                             "[THINK] Claude 正在思考...", ctx)

                # 获取用户配置
                cfg = user_config.get(from_user, {})
                cwd = cfg.get("cwd")
                model = cfg.get("model")

                # 记录历史
                _save_history(from_user, "User", text)

                print("   [THINK] Claude 处理中...", end="", flush=True)
                last_request[from_user] = now_ts
                in_flight.add(from_user)
                stats["in_flight"] = in_flight

                # 在线程池中执行 Claude 调用
                future = executor.submit(
                    ask_claude, text, from_user, sessions,
                    cwd=cwd, model=model
                )
                call_start = time.time()
                future.add_done_callback(
                    lambda f, uid=from_user, c=ctx, st=call_start:
                        _on_claude_done(f, uid, c, st)
                )

        except requests.RequestException as e:
            log.warning(f"网络错误: {e}")
            print(f"[WARN] 网络错误: {e}，3s 后重试...")
            time.sleep(3)
        except Exception as e:
            err_str = str(e)
            log.error(f"轮询出错: {err_str}")
            if "session timeout" in err_str.lower() or "-14" in err_str:
                print("[ERR] Session 已过期，请重新运行: python3 bridge.py --login")
                sys.exit(1)
            print(f"[WARN] 轮询出错: {err_str}，3s 后重试...")
            time.sleep(3)

    # 等待所有进行中的 Claude 调用完成
    if in_flight:
        print(f"[BYE] 等待 {len(in_flight)} 个进行中的任务完成...")
        executor.shutdown(wait=True, cancel_futures=False)
    else:
        executor.shutdown(wait=False)

    # 通知所有联系过的用户
    if contacted_users:
        print("[BYE] 通知用户 Claude Code 已下线...")
        for uid in contacted_users:
            try:
                send_message(base_url, token, uid,
                             "[Claude Code] 已下线，bridge 进程已终止。重新启动后将恢复服务。")
                print(f"   [BYE] 已通知 {uid}")
            except Exception as e:
                print(f"   [WARN] 通知 {uid} 失败: {e}")

    print("[OK] 已退出")


# ==========================================================================
#  入口
# ==========================================================================

if __name__ == "__main__":
    setup_logging()
    log.info("bridge 启动")

    force_login = "--login" in sys.argv
    session = None if force_login else load_session()

    if session:
        print(f"[OK] 已连接（Bot: {session.get('accountId', '?')}）\n")
    else:
        session = login()

    sessions = load_user_sessions()
    user_config = load_user_config()
    main_loop(session, sessions, user_config)
