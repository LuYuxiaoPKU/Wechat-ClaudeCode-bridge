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
REMINDERS_FILE = DATA_DIR / "reminders.json"
LOG_FILE = DATA_DIR / "bridge.log"
POLL_TIMEOUT_S = 38
RATE_LIMIT_S = 5
MAX_MSG_LEN = 2000
MAX_WORKERS = 5
WEB_PORT = 9876
STREAM_INTERVAL = 3
STREAM_MIN_CHARS = 100

_ALLOWED_ENV = os.environ.get("WCB_ALLOWED_USERS", "")
ALLOWED_USERS = set(u.strip() for u in _ALLOWED_ENV.split(",") if u.strip())

PERMISSION_MARKERS = [
    "Do you want to proceed",
    "needs your permission",
    "requires permission",
    "permission to run",
    "(y/n)",
    "proceed?",
]

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


def user_session_uuid(user_id, session_name="default"):
    return str(uuid.uuid5(
        uuid.NAMESPACE_DNS,
        f"wechat-claude-bridge:{user_id}:{session_name}"
    ))


def get_active_session_info(sessions, user_id):
    """返回 (session_name, session_uuid)，兼容旧格式"""
    entry = sessions.get(user_id)
    if entry is None:
        return "default", user_session_uuid(user_id, "default")
    if isinstance(entry, str):
        # 旧格式: user_id → uuid 字符串，迁移到新格式
        sessions[user_id] = {"active": "default", "names": ["default"]}
        save_user_sessions(sessions)
        return "default", entry
    name = entry.get("active", "default")
    return name, user_session_uuid(user_id, name)


def set_active_session(sessions, user_id, name):
    entry = sessions.setdefault(user_id, {"active": "default", "names": ["default"]})
    if isinstance(entry, str):
        entry = {"active": "default", "names": ["default"]}
        sessions[user_id] = entry
    entry["active"] = name
    if name not in entry.setdefault("names", ["default"]):
        entry["names"].append(name)
    save_user_sessions(sessions)
    return user_session_uuid(user_id, name)


def list_sessions(sessions, user_id):
    entry = sessions.get(user_id)
    if entry is None or isinstance(entry, str):
        return ["default"]
    return entry.get("names", ["default"])


def pop_session(sessions, user_id):
    """删除用户的活跃会话记录"""
    entry = sessions.get(user_id)
    if entry is None or isinstance(entry, str):
        sessions.pop(user_id, None)
    else:
        name = entry.get("active", "default")
        names = entry.get("names", ["default"])
        if name in names:
            names.remove(name)
        if not names:
            sessions.pop(user_id, None)
        else:
            entry["active"] = names[0]
            entry["names"] = names
    save_user_sessions(sessions)


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


def load_reminders():
    if REMINDERS_FILE.exists():
        return json.loads(REMINDERS_FILE.read_text())
    return []


def save_reminders(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REMINDERS_FILE.write_text(json.dumps(data, indent=2))


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


def send_typing(base_url, token, to_user_id):
    """发送'正在输入'状态"""
    try:
        api_post(base_url, "ilink/bot/sendtyping",
                 {"to_user_id": to_user_id}, token, timeout_ms=5000)
    except Exception:
        pass  # sendtyping 失败不影响主流程


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


def extract_media_url(msg):
    """从图片/文件消息中提取下载链接"""
    for item in msg.get("item_list", []):
        t = item.get("type")
        data = None
        if t == 2:
            data = item.get("image_item", {})
        elif t == 4:
            data = item.get("file_item", {})
        elif t == 5:
            data = item.get("video_item", {})
        if data is None:
            continue
        for key in ("url", "img_url", "file_url", "cdn_url", "download_url",
                     "media_url", "aes_key", "key"):
            u = data.get(key)
            if u:
                return u, data
    return None, None


def download_file(url, save_path):
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
    """
    cmd = ["claude", "-p", "--output-format", "text"]
    if extra_args:
        cmd.extend(extra_args)
    else:
        cmd.append("--permission-mode")
        cmd.append("auto")
    if model:
        cmd.extend(["--model", model])

    env = {**os.environ, "CLAUDE_CODE_SIMPLE": "1",
           "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd, env=env,
        )
    except FileNotFoundError:
        yield (False, "[ERR] 找不到 claude 命令，请确认 Claude Code 已安装")
        return

    def _write_stdin():
        try:
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        except Exception:
            pass

    threading.Thread(target=_write_stdin, daemon=True).start()

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

    start = time.time()
    sent_pos = 0
    last_output_time = start

    while not read_done.is_set() and proc.poll() is None:
        elapsed = time.time() - start
        if elapsed > timeout_s:
            proc.kill()
            yield (True, "\n[ERR] 处理超时，请简化您的问题")
            return

        current = "".join(output_chunks)
        new_text = current[sent_pos:]
        if len(new_text) >= STREAM_MIN_CHARS:
            yield (True, new_text)
            sent_pos = len(current)
            last_output_time = time.time()

        # 检测权限请求：进程存活但 10 秒无新输出 → 可能卡在权限确认
        if extra_args and "--permission-mode" not in extra_args:
            if time.time() - last_output_time > 10 and current[sent_pos:].strip():
                # 检查是否有权限请求特征
                tail = current[-500:]
                if any(m.lower() in tail.lower() for m in PERMISSION_MARKERS):
                    remaining = current[sent_pos:]
                    if remaining.strip():
                        yield (True, remaining)
                    yield (False, "[PERMISSION_REQUIRED]" + tail[-300:])
                    proc.kill()
                    return

        time.sleep(STREAM_INTERVAL)

    reader.join(timeout=5)
    proc.wait(timeout=5)

    current = "".join(output_chunks)
    remaining = current[sent_pos:]
    if remaining.strip():
        yield (True, remaining)

    stderr = "".join(stderr_chunks).strip()
    if proc.returncode != 0:
        yield (True, f"\n[ERR] {stderr or '(无错误输出)'}")
    else:
        yield (False, current.strip())


def ask_claude(text, user_id, sessions, cwd=None, model=None,
               permission_mode="auto", on_stream=None):
    """
    调用 claude CLI 处理消息。返回 (output, permission_pending, permission_text)。

    permission_pending=True 表示 Claude 请求权限确认，需要用户回复。
    """
    session_name, session_id = get_active_session_info(sessions, user_id)
    has_session = session_name in (sessions.get(user_id, {}) if isinstance(
        sessions.get(user_id), dict) else {}).get("names", ["default"]
    ) if isinstance(sessions.get(user_id), dict) else user_id in sessions

    extra = []
    if permission_mode == "auto":
        extra = ["--permission-mode", "auto"]
    else:
        extra = []  # 使用 default 模式

    def _call(extra_args):
        acc = ""
        perm_text = ""
        for is_partial, chunk in run_claude_stream(
            text, cwd=cwd, model=model, extra_args=extra_args
        ):
            if is_partial:
                if chunk.startswith("[PERMISSION_REQUIRED]"):
                    perm_text = chunk[len("[PERMISSION_REQUIRED]"):]
                    continue
                if on_stream:
                    on_stream(chunk, True)
                acc += chunk
            else:
                if chunk.startswith("[PERMISSION_REQUIRED]"):
                    perm_text = chunk[len("[PERMISSION_REQUIRED]"):]
                    continue
                acc = chunk
        return acc.strip(), perm_text

    # 尝试 resume
    if has_session:
        output, perm = _call(extra + ["--resume", session_id])
        if perm:
            return output, True, perm
        if output and not output.startswith("[ERR]"):
            return output or "（Claude 无回复）", False, ""
        pop_session(sessions, user_id)
        has_session = False

    # 新会话
    output, perm = _call(extra + ["--session-id", session_id])
    if perm:
        return output, True, perm
    if output.startswith("[ERR]"):
        return output, False, ""

    if not has_session:
        set_active_session(sessions, user_id, session_name)

    return output or "（Claude 无回复）", False, ""


# ==========================================================================
#  命令处理
# ==========================================================================


def handle_command(stripped, from_user, user_config, sessions,
                   base_url, token, ctx):
    """返回 (handled, reply_text)"""
    cfg = user_config.setdefault(from_user, {})

    # ---- /help ----
    if stripped == "/help":
        return True, (
            "[WeChat-Claude-Bridge]\n"
            "/help                   — 显示此帮助\n"
            "/cwd <path>             — 设置工作目录\n"
            "/pwd                    — 查看当前工作目录\n"
            "/new <name>             — 新建命名会话\n"
            "/list                   — 列出所有会话\n"
            "/switch <name>          — 切换活跃会话\n"
            "/clear                  — 清除当前会话\n"
            "/status                 — 查看 bridge 运行状态\n"
            "/model <opus|sonnet|haiku> — 切换模型\n"
            "/mode <auto|ask>        — 切换权限模式\n"
            "/exec <shell命令>        — 在工作目录执行命令\n"
            "/remind <时间> <消息>    — 设置提醒\n"
            "/cleanup <target>       — 清理缓存\n"
        )

    # ---- /cwd /dir /pwd ----
    if stripped.startswith("/cwd ") or stripped.startswith("/dir "):
        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            p = Path(parts[1].strip()).expanduser().resolve()
            if p.is_dir():
                cfg["cwd"] = str(p)
                save_user_config(user_config)
                return True, f"[OK] 工作目录已设置为: {p}"
            return True, f"[ERR] 目录不存在: {p}"
        return True, "[USAGE] /cwd <path>"

    if stripped in ("/cwd", "/dir", "/pwd"):
        cwd = cfg.get("cwd", "(未设置，使用 bridge 进程目录)")
        return True, f"[CWD] {cwd}"

    # ---- /new ----
    if stripped.startswith("/new "):
        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            name = parts[1].strip()
            if not name or " " in name:
                return True, "[USAGE] /new <名称>（名称不能含空格）"
            set_active_session(sessions, from_user, name)
            sess_id = user_session_uuid(from_user, name)
            return True, f"[OK] 已创建并切换到会话: {name}"

    if stripped == "/new":
        return True, "[USAGE] /new <名称>"

    # ---- /list ----
    if stripped == "/list":
        names = list_sessions(sessions, from_user)
        _, active_name = get_active_session_info(sessions, from_user)
        lines = [f"* {n}" if n == active_name else f"  {n}" for n in names]
        return True, "[SESSIONS]\n" + "\n".join(lines)

    # ---- /switch ----
    if stripped.startswith("/switch "):
        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            name = parts[1].strip()
            names = list_sessions(sessions, from_user)
            if name not in names:
                return True, f"[ERR] 会话不存在: {name}（/list 查看所有会话）"
            set_active_session(sessions, from_user, name)
            return True, f"[OK] 已切换到会话: {name}"
        return True, "[USAGE] /switch <名称>"

    if stripped == "/switch":
        return True, "[USAGE] /switch <名称>"

    # ---- /clear ----
    if stripped == "/clear":
        pop_session(sessions, from_user)
        return True, "[OK] 当前会话已清除"

    # ---- /model ----
    if stripped.startswith("/model "):
        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            m = parts[1].strip().lower()
            if m in ("opus", "sonnet", "haiku"):
                cfg["model"] = m
                save_user_config(user_config)
                pop_session(sessions, from_user)
                return True, f"[OK] 模型已切换为 {m}，会话已重置"
            return True, "[USAGE] /model <opus|sonnet|haiku>"

    if stripped == "/model":
        m = cfg.get("model", "默认")
        return True, f"[MODEL] 当前模型: {m}"

    # ---- /mode ----
    if stripped.startswith("/mode "):
        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            m = parts[1].strip().lower()
            if m in ("auto", "ask"):
                cfg["permission_mode"] = m
                save_user_config(user_config)
                desc = "自动批准" if m == "auto" else "每次询问确认（审批请求将转发到微信）"
                return True, f"[OK] 权限模式: {m}（{desc}）"
            return True, "[USAGE] /mode <auto|ask>"

    if stripped == "/mode":
        m = cfg.get("permission_mode", "auto")
        return True, f"[MODE] 当前权限模式: {m}"

    # ---- /exec ----
    if stripped.startswith("/exec "):
        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            shell_cmd = parts[1].strip()
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

    # ---- /cleanup ----
    if stripped.startswith("/cleanup"):
        parts = stripped.split(maxsplit=1)
        target = parts[1].strip().lower() if len(parts) == 2 else ""
        if target in ("", "help"):
            return True, (
                "[USAGE] /cleanup <target>\n"
                "  media   — 删除所有下载的图片/文件\n"
                "  history — 删除你的消息历史记录\n"
                "  all     — 删除 media + history"
            )

        results = []
        if target in ("media", "all"):
            media_dir = DATA_DIR / "media"
            n = 0
            if media_dir.is_dir():
                for f in media_dir.iterdir():
                    try:
                        f.unlink()
                        n += 1
                    except Exception:
                        pass
            results.append(f"media: 已删除 {n} 个文件")

        if target in ("history", "all"):
            history_dir = DATA_DIR / "history"
            user_file = history_dir / f"{from_user}.md"
            n = 0
            if user_file.exists():
                try:
                    user_file.unlink()
                    n = 1
                except Exception:
                    pass
            results.append(f"history: 已删除 {n} 个文件")

        if not results:
            return True, f"[USAGE] 未知 target: {target}，可选: media / history / all"
        return True, "[CLEANUP]\n" + "\n".join(results)

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
#  Web 控制台 + Push API
# ==========================================================================


class WebHandler(BaseHTTPRequestHandler):
    stats_ref = None
    push_callback = None  # (user_id, text) → None

    def log_message(self, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send_json({"status": "ok"})
        if self.path in ("/", "/stats"):
            s = self.stats_ref or {}
            uptime = time.time() - s.get("start_time", time.time())
            h, m_ = divmod(int(uptime), 3600)
            m, sec = divmod(m_, 60)
            return self._send_json({
                "uptime": f"{h}h {m}m {sec}s",
                "total_calls": s.get("total_calls", 0),
                "in_flight": len(s.get("in_flight", set())),
                "active_users": len(s.get("users_seen", set())),
                "recent_messages": s.get("recent", [])[-20:],
            })
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/push" and self.push_callback:
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                user_id = body.get("user_id", "")
                text = body.get("text", "")
                if user_id and text:
                    type(self).push_callback(user_id, text)
                    return self._send_json({"status": "ok"})
                return self._send_json({"error": "missing user_id or text"}, 400)
            except Exception as e:
                return self._send_json({"error": str(e)}, 400)
        self._send_json({"error": "not found"}, 404)


def run_web(stats, push_cb=None):
    handler = type("_H", (WebHandler,), {
        "stats_ref": stats, "push_callback": push_cb
    })
    try:
        server = HTTPServer(("127.0.0.1", WEB_PORT), handler)
        server.serve_forever()
    except Exception as e:
        log.warning(f"Web 控制台启动失败: {e}")


# ==========================================================================
#  提醒任务
# ==========================================================================


def check_reminders(reminders, base_url, token, executor, sessions, user_config):
    """检查并触发到期提醒"""
    now = time.time()
    triggered = []
    for i, r in enumerate(reminders):
        if now >= r["at"]:
            uid = r["user_id"]
            text = r["text"]
            triggered.append(i)

            def _send_reminder():
                _, session_id = get_active_session_info(sessions, uid)
                output, _, _ = ask_claude(
                    text, uid, sessions,
                    cwd=user_config.get(uid, {}).get("cwd"),
                    model=user_config.get(uid, {}).get("model"),
                    permission_mode=user_config.get(uid, {}).get("permission_mode", "auto"),
                )
                for chunk in split_long_text(output):
                    send_message(base_url, token, uid, f"[REMINDER] {chunk}")

            executor.submit(_send_reminder)

            # 重复提醒则更新下次时间
            if r.get("repeat"):
                r["at"] = now + r["repeat"]
            else:
                pass  # 标记删除

    # 删除已触发的非重复提醒
    for i in reversed(triggered):
        if not reminders[i].get("repeat"):
            reminders.pop(i)

    if triggered:
        save_reminders(reminders)


def reminder_thread_fn(base_url, token, executor, sessions, user_config):
    """后台线程：每 30s 检查提醒"""
    while True:
        try:
            reminders = load_reminders()
            if reminders:
                check_reminders(reminders, base_url, token, executor,
                                sessions, user_config)
        except Exception as e:
            log.warning(f"提醒检查出错: {e}")
        time.sleep(30)


# ==========================================================================
#  主循环
# ==========================================================================


def main_loop(session, sessions, user_config):
    token = session["token"]
    base_url = session["baseUrl"]
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)

    stats = {
        "start_time": time.time(),
        "total_calls": 0,
        "in_flight": set(),
        "users_seen": set(),
        "recent": [],
    }

    # HTTP push 回调
    def _on_push(user_id, text):
        executor.submit(
            ask_claude, text, user_id, sessions,
            cwd=user_config.get(user_id, {}).get("cwd"),
            model=user_config.get(user_id, {}).get("model"),
            permission_mode=user_config.get(user_id, {}).get("permission_mode", "auto"),
        )

    web_thread = threading.Thread(
        target=run_web, args=(stats, _on_push), daemon=True
    )
    web_thread.start()

    # 提醒后台线程
    remind_thread = threading.Thread(
        target=reminder_thread_fn,
        args=(base_url, token, executor, sessions, user_config),
        daemon=True,
    )
    remind_thread.start()

    running = True

    def on_sigint(sig, frame):
        nonlocal running
        print("\n\n[BYE] 正在退出...")
        running = False

    signal.signal(signal.SIGINT, on_sigint)

    print(f"[START] 开始长轮询收消息（Ctrl+C 退出）")
    print(f"[START] Web 控制台: http://127.0.0.1:{WEB_PORT}")
    print(f"[START] HTTP Push: POST http://127.0.0.1:{WEB_PORT}/push\n")

    buf = ""
    contacted_users = set()
    last_request = {}
    in_flight = set()
    pending_permission = {}  # user_id → session_name

    history_dir = DATA_DIR / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    media_dir = DATA_DIR / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    def _save_history(uid, role, content):
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            (history_dir / f"{uid}.md").open("a", encoding="utf-8").write(
                f"## {ts} [{role}]\n{content}\n\n"
            )
        except Exception:
            pass

    def _send_reply(from_user, reply, ctx):
        chunks = split_long_text(reply)
        for chunk in chunks:
            try:
                send_message(base_url, token, from_user, chunk, ctx)
            except Exception as e:
                log.warning(f"发送回复给 {from_user} 失败: {e}")
        return len(chunks)

    def _on_claude_done(future, from_user, ctx, start_time):
        try:
            reply = future.result()
        except Exception as e:
            reply = f"[ERR] {e}"

        n_chunks = _send_reply(from_user, reply, ctx)

        elapsed = time.time() - start_time
        preview = reply[:80].replace("\n", " ")
        stats["total_calls"] += 1
        stats["recent"].append((
            time.strftime("%H:%M:%S"), from_user, preview,
            f"{elapsed:.1f}s" + (f" [{n_chunks}条]" if n_chunks > 1 else "")
        ))
        if len(stats["recent"]) > 200:
            stats["recent"] = stats["recent"][-200:]
        in_flight.discard(from_user)
        stats["in_flight"] = in_flight
        _save_history(from_user, "Claude", reply)
        print(f"   [OK] [{elapsed:.1f}s] {preview}{'...' if len(reply) > 80 else ''}"
              f"{' [' + str(n_chunks) + '条]' if n_chunks > 1 else ''}")

    while running:
        try:
            resp = get_updates(base_url, token, buf)
            if resp.get("get_updates_buf"):
                buf = resp["get_updates_buf"]

            for msg in resp.get("msgs", []):
                if msg.get("message_type") != 1:
                    log.debug(f"非文本消息: {json.dumps(msg, ensure_ascii=False)[:500]}")
                    # 图片消息：尝试下载
                    if msg.get("message_type") == 2:
                        from_user_img = msg.get("from_user_id", "")
                        ctx_img = msg.get("context_token", "")
                        url, meta = extract_media_url(msg)
                        if url and ALLOWED_USERS and from_user_img not in ALLOWED_USERS:
                            continue
                        if url:
                            fname = meta.get("file_name", f"img_{uuid.uuid4().hex[:8]}.jpg")
                            save_path = media_dir / fname
                            if download_file(url, save_path):
                                send_message(base_url, token, from_user_img,
                                             f"[OK] 图片已接收: {fname}", ctx_img)
                                log.info(f"图片已下载: {save_path}")
                    continue

                from_user = msg["from_user_id"]
                text = extract_text(msg)
                ctx = msg.get("context_token", "")
                contacted_users.add(from_user)
                stats["users_seen"].add(from_user)

                ts = time.strftime("%H:%M:%S")
                print(f"[MSG] [{ts}] {from_user}")
                print(f"   {text}")

                # 白名单
                if ALLOWED_USERS and from_user not in ALLOWED_USERS:
                    send_message(base_url, token, from_user,
                                 "[ERR] 你没有权限使用此 Bot", ctx)
                    continue

                stripped = text.strip()
                cfg = user_config.setdefault(from_user, {})

                # ---- 待处理的权限确认 ----
                if from_user in pending_permission:
                    session_name = pending_permission.pop(from_user)
                    answer = stripped.strip().lower()
                    if answer in ("yes", "y", "是", "允许", "同意", "ok", "no", "n",
                                  "否", "拒绝"):
                        _, session_id = get_active_session_info(sessions, from_user)
                        # 恢复权限确认的会话
                        set_active_session(sessions, from_user, session_name)
                        permission_prompt = f"The user responded: {answer}"
                        print(f"   [PERM] {from_user} → {answer}")
                        future = executor.submit(
                            ask_claude, permission_prompt, from_user, sessions,
                            cwd=cfg.get("cwd"), model=cfg.get("model"),
                            permission_mode="auto",
                        )
                        future.add_done_callback(
                            lambda f, uid=from_user, c=ctx, st=time.time():
                                _on_claude_done(f, uid, c, st)
                        )
                        in_flight.add(from_user)
                        stats["in_flight"] = in_flight
                    else:
                        send_message(base_url, token, from_user,
                                     "[PERM] 请回复 yes/no 确认权限", ctx)
                        pending_permission[from_user] = session_name
                    continue

                # ---- 内置命令 ----
                handled, reply = handle_command(
                    stripped, from_user, user_config, sessions,
                    base_url, token, ctx
                )
                if handled:
                    send_message(base_url, token, from_user, reply, ctx)
                    print(f"   [CMD] {reply[:80]}")
                    continue

                # ---- /status ----
                if stripped == "/status":
                    uptime_s = time.time() - stats["start_time"]
                    h, m_ = divmod(int(uptime_s), 3600)
                    m, s = divmod(m_, 60)
                    _, active_name = get_active_session_info(sessions, from_user)
                    send_message(base_url, token, from_user,
                                 f"[STATUS]\n"
                                 f"运行时间: {h}h {m}m {s}s\n"
                                 f"总调用数: {stats['total_calls']}\n"
                                 f"处理中:   {len(in_flight)}\n"
                                 f"活跃会话: {active_name}\n"
                                 f"历史用户: {len(stats['users_seen'])}", ctx)
                    continue

                # ---- /remind ----
                if stripped.startswith("/remind "):
                    parts = stripped[len("/remind "):].strip()
                    try:
                        # 格式: /remind 30m 消息  或  /remind 9:00 消息
                        time_str, reminder_text = parts.split(maxsplit=1)
                        at_time = None
                        repeat = None
                        if ":" in time_str:
                            # 每天固定时间: 9:00
                            hh, mm = map(int, time_str.split(":"))
                            now_ts = time.time()
                            target = time.mktime(time.localtime()[:3] +
                                                 (0, 0, 0, 0, 0, 0)) + hh * 3600 + mm * 60
                            if target <= now_ts:
                                target += 86400
                            at_time = target
                            repeat = 86400
                        else:
                            # 相对时间: 30m, 2h
                            import re
                            m = re.match(r"(\d+)\s*(m|min|h|hour)", time_str)
                            if m:
                                val = int(m.group(1))
                                unit = m.group(2)
                                seconds = val * 60 if unit in ("m", "min") else val * 3600
                                at_time = time.time() + seconds
                    except Exception:
                        send_message(base_url, token, from_user,
                                     "[USAGE] /remind <时间> <消息>\n"
                                     "例: /remind 30m 检查部署\n"
                                     "    /remind 9:00 每日站会", ctx)
                        continue

                    if at_time:
                        reminders = load_reminders()
                        reminders.append({
                            "user_id": from_user,
                            "at": at_time,
                            "text": reminder_text,
                            "repeat": repeat,
                        })
                        save_reminders(reminders)
                        ts_fmt = time.strftime("%H:%M", time.localtime(at_time))
                        send_message(base_url, token, from_user,
                                     f"[OK] 提醒已设置: {ts_fmt} — {reminder_text}", ctx)
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
                send_message(base_url, token, from_user,
                             "[THINK] Claude 正在思考...", ctx)
                send_typing(base_url, token, from_user)

                _save_history(from_user, "User", text)

                print("   [THINK] Claude 处理中...", end="", flush=True)
                last_request[from_user] = now_ts
                in_flight.add(from_user)
                stats["in_flight"] = in_flight
                perm_mode = cfg.get("permission_mode", "auto")

                def _claude_task(uid, txt, cwd, mdl, p_mode):
                    output, perm_pending, perm_text = ask_claude(
                        txt, uid, sessions, cwd=cwd, model=mdl,
                        permission_mode=p_mode,
                    )
                    if perm_pending:
                        send_message(base_url, token, uid,
                                     f"[PERM] Claude 请求权限:\n{perm_text}"
                                     f"\n\n回复 yes/no", ctx)
                        pending_permission[uid] = \
                            get_active_session_info(sessions, uid)[0]
                        return "[权限请求已转发，等待确认]"
                    return output

                cwd = cfg.get("cwd")
                model = cfg.get("model")
                future = executor.submit(
                    _claude_task, from_user, text, cwd, model, perm_mode
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

    if in_flight:
        print(f"[BYE] 等待 {len(in_flight)} 个进行中的任务完成...")
        executor.shutdown(wait=True, cancel_futures=False)
    else:
        executor.shutdown(wait=False)

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
