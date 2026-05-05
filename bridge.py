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
TERMS_FILE = DATA_DIR / "terms_accepted"
SESSIONS_FILE = DATA_DIR / "sessions.json"
USER_CONFIG_FILE = DATA_DIR / "user_config.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
WATCHDOG_FILE = DATA_DIR / "watchdog.json"
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
    """返回 (session_name, session_uuid)，兼容旧格式。支持外部 UUID"""
    entry = sessions.get(user_id)
    if entry is None:
        return "default", user_session_uuid(user_id, "default")
    if isinstance(entry, str):
        sessions[user_id] = {"active": "default", "names": ["default"]}
        save_user_sessions(sessions)
        return "default", entry
    name = entry.get("active", "default")
    # 外部接入的 UUID（/attach 设置的）
    ext_map = entry.get("_external", {})
    if name in ext_map:
        return name, ext_map[name]
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
    if hasattr(TOKEN_FILE, "chmod"):
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
                "savedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
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
#  系统监控 (watchdog)
# ==========================================================================


# 跨平台系统监控：优先使用 psutil，回退到原生接口
try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


def _read_meminfo():
    """跨平台读取内存信息，返回 (total_kb, available_kb)"""
    if _HAS_PSUTIL:
        mem = psutil.virtual_memory()
        return mem.total // 1024, mem.available // 1024

    # Linux fallback
    total = available = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1])
                if total and available:
                    break
    except Exception:
        pass

    # macOS fallback via sysctl
    if total == 0 and sys.platform == "darwin":
        try:
            r = subprocess.run(["sysctl", "-n", "hw.memsize"],
                               capture_output=True, text=True, timeout=5)
            total = int(r.stdout.strip()) // 1024
            # 粗略估算可用内存 (不精确, 但可用)
            import re
            r2 = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
            pages = {}
            for line in r2.stdout.splitlines():
                m = re.match(r'(.+):\s+(\d+)\.?', line.strip().replace('"', ''))
                if m:
                    pages[m.group(1).strip()] = int(m.group(2))
            page_size = 16384  # ARM Mac default
            free_pages = pages.get("Pages free", 0) + pages.get("Pages inactive", 0)
            available = free_pages * page_size // 1024
        except Exception:
            pass

    return total, available


def _detect_disks():
    """跨平台自动检测真实磁盘挂载点"""
    if _HAS_PSUTIL:
        pseudo_fs = {"proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup",
                      "cgroup2", "pstore", "bpf", "fusectl", "securityfs",
                      "debugfs", "tracefs", "configfs", "hugetlbfs", "mqueue",
                      "ramfs", "overlay", "squashfs", "autofs", "binfmt_misc"}
        skip_prefixes = ("/sys/", "/proc/", "/dev/", "/run/", "/snap/", "/var/lib/")
        disks = []
        for part in psutil.disk_partitions():
            if part.fstype and part.fstype.lower() in pseudo_fs:
                continue
            if any(part.mountpoint.startswith(p) for p in skip_prefixes):
                continue
            if part.device and part.mountpoint:
                disks.append(part.mountpoint)
        return sorted(set(disks)) if disks else ["/"]

    # Linux fallback: /proc/mounts
    if sys.platform == "linux":
        pseudo_fs = {
            "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup",
            "cgroup2", "pstore", "bpf", "fusectl", "securityfs", "debugfs",
            "tracefs", "configfs", "hugetlbfs", "mqueue", "ramfs", "overlay",
        }
        skip_prefixes = ("/sys/", "/proc/", "/dev/", "/run/", "/snap/")
        disks = []
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    dev, mp, fs_type = parts[0], parts[1], parts[2]
                    if fs_type in pseudo_fs:
                        continue
                    if any(mp.startswith(p) for p in skip_prefixes):
                        continue
                    if dev.startswith("/dev/"):
                        disks.append(mp)
        except Exception:
            pass
        return sorted(set(disks)) if disks else ["/"]

    # macOS fallback: parse mount output
    if sys.platform == "darwin":
        disks = []
        try:
            r = subprocess.run(["mount", "-t", "apfs,hfs,exfat,msdos"],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[2].startswith("/"):
                    disks.append(parts[2])
        except Exception:
            pass
        return sorted(set(disks)) if disks else ["/"]

    # Windows fallback: enumerate drive letters
    if sys.platform == "win32":
        import string
        disks = []
        for letter in string.ascii_uppercase:
            p = f"{letter}:\\"
            if os.path.exists(p):
                try:
                    shutil.disk_usage(p)
                    disks.append(p)
                except Exception:
                    pass
        return disks if disks else ["C:\\"]

    return ["/"]


def collect_metrics(disk_paths=None):
    """跨平台采集系统指标，返回 dict"""
    if disk_paths is None:
        disk_paths = _detect_disks()
    metrics = {"timestamp": time.time(), "alerts": []}

    # ---- CPU ----
    cpu_pct = -1.0
    cpu_load = -1
    if _HAS_PSUTIL:
        cpu_pct = psutil.cpu_percent(interval=0.5)
        cpu_load = cpu_pct  # psutil 直接给百分比, 保持兼容
    else:
        try:
            load = os.getloadavg()[0]
            cpu_count = os.cpu_count() or 1
            cpu_load = round(load, 2)
            cpu_pct = round((load / cpu_count) * 100, 1)
        except Exception:
            cpu_load = -1
            cpu_pct = -1
    metrics["cpu_load"] = cpu_load
    metrics["cpu_percent"] = cpu_pct

    # ---- 内存 ----
    total_kb, avail_kb = _read_meminfo()
    if total_kb > 0:
        metrics["memory_total_gb"] = round(total_kb / 1024 / 1024, 1)
        metrics["memory_used_gb"] = round((total_kb - avail_kb) / 1024 / 1024, 1)
        metrics["memory_percent"] = round(((total_kb - avail_kb) / total_kb) * 100, 1)
    else:
        metrics["memory_percent"] = -1

    # ---- 磁盘 ----
    metrics["disks"] = {}
    for p in disk_paths:
        try:
            usage = shutil.disk_usage(p)
            pct = round((usage.used / usage.total) * 100, 1)
            metrics["disks"][p] = {
                "total_gb": round(usage.total / 1024**3, 1),
                "used_gb": round(usage.used / 1024**3, 1),
                "percent": pct,
            }
        except Exception:
            metrics["disks"][p] = {"error": "unable to read"}

    return metrics


def _progress_bar(pct, width=10):
    """生成美观进度条: [████████  ] 80%"""
    if pct < 0:
        return f"[{'?' * width}] ???%"
    filled = min(int(pct / 100 * width), width)
    empty = width - filled
    bar = "█" * filled + " " * empty
    return f"[{bar}] {pct:.0f}%"


def check_watchdog(user_id=None):
    """检查看门狗阈值，返回警报文本列表"""
    config = load_watchdog_config()
    if not config.get("enabled"):
        return None, []

    thresholds = config.get("thresholds", {})
    disk_paths = config.get("disk_paths", ["/"])
    metrics = collect_metrics(disk_paths)
    alerts = []

    cpu_thresh = thresholds.get("cpu_percent", 80)
    if metrics["cpu_percent"] > cpu_thresh:
        bar = _progress_bar(metrics["cpu_percent"])
        alerts.append(f"{'CPU':<5} {bar}  阈值 {cpu_thresh}%")

    mem_thresh = thresholds.get("memory_percent", 90)
    if metrics["memory_percent"] > mem_thresh:
        bar = _progress_bar(metrics["memory_percent"])
        alerts.append(
            f"{'MEM':<5} {bar}  "
            f"{metrics.get('memory_used_gb','?')}/{metrics.get('memory_total_gb','?')}G"
        )

    disk_thresh = thresholds.get("disk_percent", 90)
    for path, info in metrics.get("disks", {}).items():
        if "percent" in info and info["percent"] > disk_thresh:
            bar = _progress_bar(info["percent"])
            alerts.append(
                f"{'DISK':<5} {path:<10} {bar}  "
                f"{info['used_gb']}/{info['total_gb']}G"
            )

    # 更新最后检查时间
    config["last_check"] = time.time()
    config["last_metrics"] = metrics
    save_watchdog_config(config)

    return config, alerts


def load_watchdog_config():
    if WATCHDOG_FILE.exists():
        return json.loads(WATCHDOG_FILE.read_text())
    return {
        "enabled": False,
        "interval_minutes": 5,
        "disk_paths": _detect_disks(),
        "alert_cooldown_minutes": 30,
        "thresholds": {"cpu_percent": 80, "memory_percent": 90, "disk_percent": 90},
        "alert_user_id": None,
        "last_check": None,
        "last_alert": None,
        "last_metrics": None,
    }


def save_watchdog_config(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WATCHDOG_FILE.write_text(json.dumps(data, indent=2))


def watchdog_thread_fn(base_url, token):
    """后台线程：按间隔执行系统监控"""
    while True:
        try:
            cfg = load_watchdog_config()
            if cfg.get("enabled") and cfg.get("alert_user_id"):
                interval = cfg.get("interval_minutes", 5) * 60
                now = time.time()
                last_check = cfg.get("last_check", 0)
                if now - last_check >= interval:
                    _, alerts = check_watchdog()
                    if alerts:
                        last_alert = cfg.get("last_alert", 0)
                        cooldown = cfg.get("alert_cooldown_minutes", 30) * 60
                        if now - last_alert >= cooldown:
                            msg = "[WATCHDOG ALERT]\n```\n" + "\n".join(alerts) + "\n```"
                            send_message(base_url, token, cfg["alert_user_id"], msg)
                            cfg["last_alert"] = now
                            save_watchdog_config(cfg)
                            log.warning(f"Watchdog 警报: {alerts}")
        except Exception as e:
            log.warning(f"Watchdog 出错: {e}")
        time.sleep(30)


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
            "[ WeChat-Claude-Bridge ]\n"
            "\n"
            "```\n"
            "[ 会话 & 模型 ]\n"
            "  /new <name>              新建命名会话\n"
            "  /list                    列出所有会话\n"
            "  /switch <name>           切换活跃会话\n"
            "  /attach <uuid> [name]   接入外部会话\n"
            "  /clear                   清除当前会话\n"
            "  /model <opus|sonnet|haiku> 切换模型\n"
            "  /mode <auto|ask>         权限模式\n"
            "\n"
            "[ 工作目录 ]\n"
            "  /cwd <path>              设置工作目录\n"
            "  /pwd                     查看当前目录\n"
            "\n"
            "[ 系统 & 工具 ]\n"
            "  /cpu                     查看 CPU 负载\n"
            "  /mem                     查看内存使用\n"
            "  /disk                    查看磁盘使用\n"
            "  /ls [path]               列出目录内容\n"
            "  /top [cpu|mem]           查看进程 Top20\n"
            "  /exec <shell cmd>        执行命令\n"
            "  /status                  运行状态\n"
            "  /watchdog <cmd>          系统监控\n"
            "  /remind <时间> <消息>     定时提醒\n"
            "  /history [N]            回看最近 N 轮对话\n"
            "  /cleanup <target>        清理缓存\n"
            "  /login                   重新扫码登录\n"
            "```\n"
            "//<cmd>  绕过桥接，直接发送给 Claude Code CLI"
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
        label = "PWD" if stripped == "/pwd" else "CWD"
        return True, f"[{label}] {cwd}"

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
        lines = [f"[SESSIONS] {len(names)} 个会话"]
        for i, n in enumerate(names, 1):
            marker = ">>" if n == active_name else "  "
            lines.append(f" {marker} {i}. {n}")
        return True, "\n".join(lines)

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

    # ---- /attach <session_uuid> [name] ----
    if stripped.startswith("/attach "):
        parts = stripped.split()
        if len(parts) >= 2:
            ext_id = parts[1]
            # 验证是否为合法 UUID 格式
            try:
                uuid.UUID(ext_id)
            except ValueError:
                return True, f"[ERR] 无效的 session UUID: {ext_id}"
            name = parts[2] if len(parts) >= 3 else f"ext-{ext_id[:8]}"
            # 直接将外部 UUID 作为会话名对应的 UUID 存入
            entry = sessions.setdefault(from_user,
                                        {"active": "default", "names": ["default"]})
            if isinstance(entry, str):
                entry = {"active": "default", "names": ["default"]}
                sessions[from_user] = entry
            entry["active"] = name
            if name not in entry.setdefault("names", ["default"]):
                entry["names"].append(name)
            # 绕过 uuid5 生成，直接存储外部 UUID 到会话映射
            cfg_sess = sessions[from_user]
            # 用一个特殊字段存储外部 UUIDs
            ext_map = cfg_sess.setdefault("_external", {})
            ext_map[name] = ext_id
            save_user_sessions(sessions)
            cwd = cfg.get("cwd", os.getcwd())
            return True, (
                f"[OK] 已接入外部会话: {name}\n"
                f"UUID: {ext_id}\n"
                f"工作目录: {cwd}\n"
                f"\n"
                f"如需切换目录，先 /cwd <path> 再发消息继续对话"
            )
        return True, "[USAGE] /attach <session_uuid> [名称]"

    if stripped == "/attach":
        cwd = cfg.get("cwd", os.getcwd())
        return True, (
            f"[USAGE] /attach <session_uuid> [名称]\n"
            f"\n"
            f"当前工作目录: {cwd}\n"
            f"\n"
            f"1. 在 Claude Code CLI 中查看 session UUID\n"
            f"2. 确保 /cwd 指向正确的项目目录\n"
            f"3. /attach <uuid> 接入外部会话"
        )

    # ---- /clear ----
    if stripped == "/clear":
        _, active_name = get_active_session_info(sessions, from_user)
        pop_session(sessions, from_user)
        return True, f"[OK] 会话 '{active_name}' 已清除"

    if stripped.startswith("/clear "):
        name = stripped.split(maxsplit=1)[1].strip()
        names = list_sessions(sessions, from_user)
        if name not in names:
            return True, f"[ERR] 会话不存在: {name}（/list 查看所有会话）"
        _, active_name = get_active_session_info(sessions, from_user)
        if name == active_name:
            pop_session(sessions, from_user)
        else:
            entry = sessions.get(from_user)
            if isinstance(entry, dict):
                entry["names"].remove(name)
                entry["_external"].pop(name, None) if "_external" in entry else None
                save_user_sessions(sessions)
        return True, f"[OK] 会话 '{name}' 已清除"

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
    # ---- /top 进程列表 ----
    if stripped == "/top" or stripped.startswith("/top "):
        sort_by = stripped[4:].strip().lower() if len(stripped) > 4 else "cpu"
        if sort_by not in ("cpu", "mem", "memory"):
            return True, "[USAGE] /top [cpu|mem]  默认按 CPU 排序"
        if not _HAS_PSUTIL:
            return True, "[ERR] /top 需要 psutil 库"

        sort_key = "memory_percent" if sort_by in ("mem", "memory") else "cpu_percent"
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                info = p.info
                if info["cpu_percent"] > 0 or info["memory_percent"] > 0.1:
                    procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        procs.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
        procs = procs[:20]

        label = "MEM%" if sort_key == "memory_percent" else "CPU%"
        lines = [f"```", f"{'PID':>8} {'NAME':<20} {'CPU%':>6} {'MEM%':>6}"]
        for p in procs:
            name = (p["name"] or "?")[:20]
            lines.append(
                f"{p['pid']:>8} {name:<20} "
                f"{p['cpu_percent'] or 0:>5.1f} {p['memory_percent'] or 0:>5.1f}"
            )
        lines.append(f"```")
        lines.append(f"排序: {label} | 显示 {len(procs)} 个进程")
        return True, "\n".join(lines)

    # ---- /ls 目录列表 ----
    if stripped == "/ls" or stripped.startswith("/ls "):
        target = stripped[3:].strip() or "."
        exec_cwd = cfg.get("cwd") or os.getcwd()
        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = Path(exec_cwd) / target
        target_path = target_path.resolve()
        if not target_path.is_dir():
            return True, f"[ERR] 目录不存在: {target_path}"
        try:
            entries = sorted(target_path.iterdir(),
                             key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            return True, f"[ERR] 无权限访问: {target_path}"
        except Exception as e:
            return True, f"[ERR] {e}"

        def _fmt_size(size):
            if size < 1024:
                return f"{size}B"
            elif size < 1024**2:
                return f"{size/1024:.1f}K"
            elif size < 1024**3:
                return f"{size/1024**2:.1f}M"
            return f"{size/1024**3:.1f}G"

        dirs, files = [], []
        for entry in entries:
            try:
                st = entry.stat()
                mtime = time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime))
                size = _fmt_size(st.st_size)
                if entry.is_dir():
                    dirs.append((entry.name, mtime))
                else:
                    files.append((entry.name, size, mtime))
            except OSError:
                if entry.is_dir():
                    dirs.append((entry.name, "?"))
                else:
                    files.append((entry.name, "?", "?"))

        lines = [f"```", f"  {target_path}"]
        for name, mtime in dirs:
            lines.append(f"  [{name}/]")
        for name, size, mtime in files:
            lines.append(f"  {name:<30} {size:>6}  {mtime}")
        lines.append(f"```")
        lines.append(f"{len(dirs)} dirs, {len(files)} files"
                      + (f" (显示前 {len(entries)} 项)" if len(entries) >= 200 else ""))
        return True, "\n".join(lines)

    # ---- /exec ----
    if stripped.startswith("/exec "):
        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            shell_cmd = parts[1].strip()
            exec_cwd = cfg.get("cwd") or os.getcwd()
            # 跨平台 shell
            if sys.platform == "win32":
                shell = "cmd"
            else:
                shell = os.environ.get("SHELL", "/bin/sh")
            try:
                result = subprocess.run(
                    shell_cmd, shell=True, capture_output=True,
                    encoding="utf-8", timeout=30, cwd=exec_cwd, executable=shell,
                    env={"PATH": os.environ.get("PATH", os.defpath),
                         "HOME": os.environ.get("HOME", os.path.expanduser("~"))},
                )
                out = result.stdout.strip() or "(无输出)"
                if result.returncode != 0:
                    err = result.stderr.strip()
                    if err:
                        out += f"\n[STDERR] {err}"
                    # 代码块包裹，保持换行可读
                    return True, f"[EXIT {result.returncode}]\n```\n{out[:1800]}\n```"
                return True, f"```\n{out[:2000]}\n```"
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

    # ---- /cpu /mem /disk 快速查询 ----
    if stripped in ("/cpu", "/mem", "/memory", "/disk", "/df"):
        metrics = collect_metrics()
        m = metrics
        lines = ["```"]
        if stripped in ("/cpu",):
            lines.append(f"{'CPU':<5} {_progress_bar(m['cpu_percent'])}  load {m.get('cpu_load','?')}")
            lines.append(f"      cores: {os.cpu_count() or '?'}")
        elif stripped in ("/mem", "/memory"):
            lines.append(f"{'MEM':<5} {_progress_bar(m['memory_percent'])}")
            lines.append(f"      {m.get('memory_used_gb','?')}G / {m.get('memory_total_gb','?')}G total")
        elif stripped in ("/disk", "/df"):
            for p, i in m.get("disks", {}).items():
                lines.append(f"{'DISK':<5} {p:<10} {_progress_bar(i.get('percent',-1))}  {i.get('used_gb','?')}/{i.get('total_gb','?')}G")
        lines.append("```")
        return True, "\n".join(lines)

    # ---- /watchdog ----
    if stripped.startswith("/watchdog"):
        parts = stripped.split(maxsplit=1)
        sub = parts[1].strip().lower() if len(parts) == 2 else ""
        wc = load_watchdog_config()

        if sub in ("", "help"):
            return True, (
                "[USAGE] /watchdog <command>\n"
                "  start [分钟]       — 开始监控（默认 5 分钟间隔）\n"
                "  stop              — 停止监控\n"
                "  status            — 查看状态和最近指标\n"
                "  config <key> <val> — 设置阈值\n"
                "  paths <p1,p2,...> — 设置监控的磁盘路径\n"
                "  now               — 立即检查一次"
            )

        if sub == "start" or sub.startswith("start "):
            interval = 5
            if sub.startswith("start "):
                try:
                    interval = int(sub.split()[1])
                except ValueError:
                    pass
            wc["enabled"] = True
            wc["interval_minutes"] = interval
            wc["alert_user_id"] = from_user
            wc["last_check"] = 0
            wc["last_alert"] = 0
            save_watchdog_config(wc)

            # 立即执行首次检查
            _, init_alerts = check_watchdog()
            wc = load_watchdog_config()  # 重新读取（check_watchdog 已更新 last_check）

            status_lines = [
                f"[OK] Watchdog 已启动",
                f"间隔: {interval} 分钟",
                f"CPU 阈值: {wc['thresholds']['cpu_percent']}%",
                f"内存阈值: {wc['thresholds']['memory_percent']}%",
                f"磁盘阈值: {wc['thresholds']['disk_percent']}%",
                f"监控磁盘: {', '.join(wc.get('disk_paths', _detect_disks()))}",
                f"告警冷却: {wc.get('alert_cooldown_minutes', 30)} 分钟",
            ]
            if init_alerts:
                status_lines.append("\n[首次检查 ALERT]\n```\n" + "\n".join(init_alerts) + "\n```")
            else:
                m = wc.get("last_metrics", {})
                if m:
                    status_lines.append("[首次检查 OK]")
                    status_lines.append("```")
                    cpu_pct = m.get('cpu_percent', -1)
                    status_lines.append(f"{'CPU':<5} {_progress_bar(cpu_pct)}  load {m.get('cpu_load','?')}")
                    mem_pct = m.get('memory_percent', -1)
                    status_lines.append(f"{'MEM':<5} {_progress_bar(mem_pct)}  {m.get('memory_used_gb','?')}/{m.get('memory_total_gb','?')}G")
                    for p, i in m.get("disks", {}).items():
                        dp = i.get("percent", -1)
                        status_lines.append(f"{'DISK':<5} {p:<10} {_progress_bar(dp)}  {i.get('used_gb','?')}/{i.get('total_gb','?')}G")
                    status_lines.append("```")
            return True, "\n".join(status_lines)

        if sub == "stop":
            wc["enabled"] = False
            save_watchdog_config(wc)
            return True, "[OK] Watchdog 已停止"

        if sub == "now":
            _, alerts = check_watchdog()
            if alerts:
                return True, "[WATCHDOG ALERT]\n```\n" + "\n".join(alerts) + "\n```"
            m = wc.get("last_metrics", {})
            if m:
                lines = ["[WATCHDOG OK]", "```"]
                cpu_pct = m.get('cpu_percent', -1)
                lines.append(f"{'CPU':<5} {_progress_bar(cpu_pct)}  load {m.get('cpu_load','?')}")
                mem_pct = m.get('memory_percent', -1)
                lines.append(f"{'MEM':<5} {_progress_bar(mem_pct)}  {m.get('memory_used_gb','?')}/{m.get('memory_total_gb','?')}G")
                for p, i in m.get("disks", {}).items():
                    dp = i.get("percent", -1)
                    lines.append(f"{'DISK':<5} {p:<10} {_progress_bar(dp)}  {i.get('used_gb','?')}/{i.get('total_gb','?')}G")
                lines.append("```")
                return True, "\n".join(lines)
            return True, "[WATCHDOG] 暂无数据，等待首次检查"

        if sub == "status":
            if not wc.get("enabled"):
                return True, "[WATCHDOG] 未启动（/watchdog start）"
            m = wc.get("last_metrics")
            lines = [
                "[WATCHDOG]",
                f"状态: 运行中 | 间隔: {wc.get('interval_minutes',5)}min"
            ]
            ts = wc.get("last_check")
            if ts:
                ago = int(time.time() - ts)
                lines.append(f"上次: {ago}s 前")
            if m:
                lines.append("```")
                cpu_pct = m.get('cpu_percent', -1)
                lines.append(f"{'CPU':<5} {_progress_bar(cpu_pct)}  load {m.get('cpu_load','?')}")
                mem_pct = m.get('memory_percent', -1)
                lines.append(f"{'MEM':<5} {_progress_bar(mem_pct)}  {m.get('memory_used_gb','?')}/{m.get('memory_total_gb','?')}G")
                for p, i in m.get("disks", {}).items():
                    dp = i.get("percent", -1)
                    lines.append(f"{'DISK':<5} {p:<10} {_progress_bar(dp)}  {i.get('used_gb','?')}/{i.get('total_gb','?')}G")
                lines.append("```")
                lines.append(f"阈值: CPU>{wc['thresholds']['cpu_percent']}% "
                             f"MEM>{wc['thresholds']['memory_percent']}% "
                             f"DISK>{wc['thresholds']['disk_percent']}%")
            return True, "\n".join(lines)

        if sub.startswith("config "):
            args = sub[len("config "):].strip().split()
            if len(args) >= 2:
                key = args[0]
                try:
                    val = float(args[1])
                except ValueError:
                    return True, f"[ERR] 值必须是数字: {args[1]}"
                valid_keys = {"cpu_percent", "memory_percent", "disk_percent",
                              "alert_cooldown_minutes", "interval_minutes"}
                if key in valid_keys:
                    if key in ("cpu_percent", "memory_percent", "disk_percent"):
                        wc["thresholds"][key] = val
                    elif key == "alert_cooldown_minutes":
                        wc["alert_cooldown_minutes"] = val
                    elif key == "interval_minutes":
                        wc["interval_minutes"] = int(val)
                    save_watchdog_config(wc)
                    return True, f"[OK] {key} = {val}"
                return True, f"[ERR] 未知配置项: {key}，可选: {', '.join(valid_keys)}"
            return True, "[USAGE] /watchdog config cpu_percent 80"

        if sub.startswith("paths "):
            paths = [p.strip() for p in sub[len("paths "):].strip().split(",") if p.strip()]
            if paths:
                wc["disk_paths"] = paths
                save_watchdog_config(wc)
                return True, f"[OK] 磁盘监控路径: {', '.join(paths)}"
            return True, "[USAGE] /watchdog paths /,/data,/home"

        return True, "[USAGE] 未知子命令（/watchdog help 查看帮助）"

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

    # Watchdog 后台线程
    wd_thread = threading.Thread(
        target=watchdog_thread_fn,
        args=(base_url, token),
        daemon=True,
    )
    wd_thread.start()

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

                # ---- //逃逸：将 /cmd 原样发送给 Claude ----
                if stripped.startswith("//"):
                    text = stripped[1:]  # 去掉一个 /，变成 /cmd

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

                # ---- /history [N] 回看对话 ----
                if stripped == "/history" or stripped.startswith("/history "):
                    n = 3
                    if stripped.startswith("/history "):
                        try:
                            n = int(stripped.split()[1])
                        except ValueError:
                            pass
                    n = max(1, min(n, 20))
                    user_file = history_dir / f"{from_user}.md"
                    if not user_file.exists():
                        send_message(base_url, token, from_user,
                                     "[HISTORY] 暂无对话记录", ctx)
                        continue
                    blocks = []
                    current_block = None
                    for line in user_file.read_text(encoding="utf-8").splitlines():
                        if line.startswith("## ") and "[User]" in line:
                            if current_block:
                                blocks.append(current_block)
                            current_block = [line]
                        elif line.startswith("## ") and "[Claude]" in line:
                            if current_block:
                                blocks.append(current_block)
                            current_block = [line]
                        elif current_block is not None:
                            current_block.append(line)
                    if current_block:
                        blocks.append(current_block)
                    recent = blocks[-n * 2:]  # n 轮对话 = 2n 个 block
                    if not recent:
                        send_message(base_url, token, from_user,
                                     "[HISTORY] 暂无对话记录", ctx)
                        continue
                    out = [f"[HISTORY] 最近 {len(recent)//2} 轮对话", "```"]
                    for b in recent:
                        out.append("\n".join(b))
                    out.append("```")
                    send_message(base_url, token, from_user, "\n".join(out), ctx)
                    continue

                # ---- /login 重新登录 ----
                if stripped == "/login":
                    def _re_login():
                        nonlocal token, base_url
                        try:
                            send_message(base_url, token, from_user,
                                         "[LOGIN] 正在获取登录二维码...", ctx)
                            qr_resp = api_get(base_url,
                                              f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}")
                            qr_url = qr_resp.get("qrcode_img_content", "")
                            qr_id = qr_resp["qrcode"]
                            send_message(base_url, token, from_user,
                                         f"[QR] 请在浏览器中打开以下链接，"
                                         f"用微信扫描二维码：\n{qr_url}", ctx)
                            deadline = time.time() + 5 * 60
                            refresh_count = 0
                            while time.time() < deadline:
                                status_resp = api_get(
                                    base_url,
                                    f"ilink/bot/get_qrcode_status?qrcode={qr_id}")
                                s = status_resp["status"]
                                if s == "wait":
                                    time.sleep(2)
                                elif s == "scaned":
                                    send_message(base_url, token, from_user,
                                                 "[SCAN] 已扫码，请在微信端确认...", ctx)
                                    time.sleep(2)
                                elif s == "expired":
                                    refresh_count += 1
                                    if refresh_count > 3:
                                        send_message(base_url, token, from_user,
                                                     "[ERR] 二维码多次过期，请重试 /login", ctx)
                                        return
                                    send_message(base_url, token, from_user,
                                                 f"[WAIT] 二维码过期，刷新中 ({refresh_count}/3)...", ctx)
                                    new_qr = api_get(
                                        base_url,
                                        f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}")
                                    qr_id = new_qr["qrcode"]
                                    qr_url = new_qr.get("qrcode_img_content", "")
                                    send_message(base_url, token, from_user,
                                                 f"[QR] 新二维码：\n{qr_url}", ctx)
                                elif s == "confirmed":
                                    new_session = {
                                        "token": status_resp["bot_token"],
                                        "baseUrl": status_resp.get("baseurl", DEFAULT_BASE_URL),
                                        "accountId": status_resp["ilink_bot_id"],
                                        "userId": status_resp["ilink_user_id"],
                                        "savedAt": time.strftime("%Y-%m-%d %H:%M:%S",
                                                                 time.localtime()),
                                    }
                                    save_session(new_session)
                                    session["token"] = new_session["token"]
                                    session["baseUrl"] = new_session["baseUrl"]
                                    session["accountId"] = new_session["accountId"]
                                    token = new_session["token"]
                                    base_url = new_session["baseUrl"]
                                    send_message(base_url, token, from_user,
                                                 f"[OK] 重新登录成功！\n"
                                                 f"Bot: {new_session['accountId']}", ctx)
                                    log.info(f"re-login: {new_session['accountId']}")
                                    return
                            send_message(base_url, token, from_user,
                                         "[ERR] 登录超时（5 分钟）", ctx)
                        except Exception as e:
                            send_message(base_url, token, from_user,
                                         f"[ERR] 登录失败: {e}", ctx)
                            log.error(f"re-login error: {e}")

                    threading.Thread(target=_re_login, daemon=True).start()
                    send_message(base_url, token, from_user,
                                 "[LOGIN] 正在后台执行重新登录，请稍候...", ctx)
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
                if stripped == "/remind":
                    send_message(base_url, token, from_user,
                                 "[USAGE] /remind <时间> <消息>\n"
                                 "  30m 检查部署      30 分钟后提醒\n"
                                 "  9:00 每日站会     每天 9:00 提醒\n"
                                 "  2h 开会            2 小时后提醒", ctx)
                    continue
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

                # ---- 未知 / 命令 → 模糊匹配 ----
                if stripped.startswith("/"):
                    ALL_COMMANDS = [
                        "help", "cwd", "pwd", "dir",
                        "new", "list", "switch", "attach", "clear",
                        "model", "mode", "exec", "status",
                        "cpu", "mem", "memory", "disk", "df",
                        "remind", "cleanup", "watchdog", "login", "ls", "top", "history",
                    ]
                    cmd_name = stripped.split()[0].lstrip("/").lower()
                    # 1. 前缀匹配（用户输入的前几个字符）
                    suggestions = []
                    for c in ALL_COMMANDS:
                        if c.startswith(cmd_name[:3]) and len(cmd_name[:3]) >= 2:
                            suggestions.append("/" + c)
                    # 2. 子串匹配
                    if not suggestions:
                        for c in ALL_COMMANDS:
                            if cmd_name[:2] in c or c[:2] in cmd_name:
                                suggestions.append("/" + c)
                    # 3. 回退
                    if not suggestions:
                        suggestions = ["/help"]
                    send_message(base_url, token, from_user,
                                 f"[?] {stripped} 不是有效命令\n"
                                 f"试试: {', '.join(suggestions[:5])}\n"
                                 f"输入 /help 查看全部", ctx)
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
#  条款确认
# ==========================================================================

TERMS_TEXT = """\
================================================================================
          !!! 核心豁免声明与动态合规告知 (GPLv3 No Warranty)
================================================================================
[重要提示] 本软件依据 GNU 通用公共许可证第 3 版 (GPLv3) 发布并受该协议保护。
本软件按"原样 (AS IS)"提供，作者不对本软件承担任何明示或默示的担保。
在配置并运行本程序之前，您必须以管理员/所有者权限仔细阅读并确认以下条款。

--------------------------------------------------------------------------------
一、 动态数据流向与跨境传输合规 (Data Flow & Cross-Border Compliance)
--------------------------------------------------------------------------------
1.1 中间件透明性 (Transparency)：本软件定位为纯粹的技术桥梁 (Middleware)，
    仅负责在用户的微信 ClawBot 接口与用户自行配置的 AI Agent 端点 (Endpoint)
    之间进行数据的透传转发。
1.2 出境风险自决 (User Responsibility)：数据的实际存储地与处理地，完全取
    决于您在下发配置中设定的 AI 服务供应商（例如：Anthropic Claude 位于美
    国，DeepSeek 位于中国）。
    您在此承诺，应尽量避免在微信交互中向本软件输入任何受法律保护的敏感个人信
    息或商业秘密。
1.3 合规责任自负：您需自行确保您的网络环境、数据出境/入境行为，以及所选用的
    AI 服务供应商，严格符合您所在司法管辖区的法律法规（如中国《个人信息保护
    法》PIPL）。因您擅自传输违规数据或选用不合规节点导致的一切法律责任，均
    由您自行承担。

--------------------------------------------------------------------------------
二、 平台合规与账号封禁风险 (Platform & API Compliance)
--------------------------------------------------------------------------------
2.1 第三方服务条款约束：您在使用本软件时，必须严格遵守微信（腾讯）及您所配
    置的 AI 服务供应商（如 Anthropic, DeepSeek 等）的相关服务条款 (ToS)。
2.2 滥用与费用限制：若因您的操作不当（如高频并发请求、批量群控、触发供应商
    风控）导致 API Key 被封禁或产生高额费用，责任由您自行承担。
2.3 微信生态风控免责：尽管本软件基于官方接口开发，但微信平台对异常的自动化
    消息交互仍保留严格的监控机制。您承诺仅将本软件用于合法、适度的个人效率
    提升。严禁用于任何骚扰、恶意营销、诈骗或破坏微信生态的行为。由此引发的
    微信号封禁或法律责任，由您自行承担。
2.4 底层依赖变更免责：本软件的运行依赖于微信官方的接口服务。若因微信官方更
    新、维护或关闭相关接口导致本软件功能失效、中断或数据丢失，作者概不负责。

--------------------------------------------------------------------------------
三、 知识产权与 GPLv3 责任限制 (GPLv3 Liability & IP)
--------------------------------------------------------------------------------
3.1 技术中立原则：本软件仅供个人学习、研究或非商业性测试使用。作者开发本软
    件出于技术探讨目的，不对您使用本软件的具体行为及产生的直接或间接后果承
    担任何法律责任。
3.2 GPLv3 免责声明：在任何情况下，作者或版权持有者均不对您因使用或无法使用
    本程序而产生的任何损害负责，包括但不限于利润损失、业务中断、计算机故障
    或数据丢失等一般性、特殊性、偶发性或必然性的损害。
3.3 AI 内容甄别义务：AI 模型生成的任何内容均由对应供应商的算法自动生成，
    其准确性、完整性和合法性未经人工核验。您需对依赖该内容做出的任何决策自
    行承担风险。严禁将本软件用于学术作弊、造谣传谣或生成违法不良信息。

================================================================================
[最终确认条款]
本人/本单位已仔细阅读并完全理解上述所有条款，特别是免除或限制责任的条款。
本人确认具备必要的技术能力以自行承担使用该软件的风险，并承诺遵守所有适用的
法律法规及第三方平台的服务条款。

如您同意并接受上述所有条款，请在下方输入指令以生成配置文件并启动服务：
（提示：输入大写或小写的指令均可，按回车键继续）

>>> I_ACCEPT_GPLv3_AND_COMPLIANCE_TERMS

================================================================================
"""


def check_terms():
    """检查用户是否已接受条款，未接受则显示条款并等待确认"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if TERMS_FILE.exists():
        return True

    print(TERMS_TEXT)
    try:
        user_input = input().strip().upper()
    except (EOFError, KeyboardInterrupt):
        print("\n[ERR] 未接受条款，程序退出。")
        sys.exit(1)

    if user_input == "I_ACCEPT_GPLV3_AND_COMPLIANCE_TERMS":
        TERMS_FILE.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        TERMS_FILE.chmod(0o600) if hasattr(TERMS_FILE, "chmod") else None
        print("\n[OK] 条款已接受。正在启动 bridge...\n")
        return True
    else:
        print(f"\n[ERR] 输入 '{user_input}' 无效。程序退出。")
        print("如需接受条款，请重新运行并在提示时输入:")
        print("  I_ACCEPT_GPLv3_AND_COMPLIANCE_TERMS")
        sys.exit(1)


# ==========================================================================
#  入口
# ==========================================================================

if __name__ == "__main__":
    check_terms()
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

    # 上线通知：向所有历史用户发送上线消息
    known_users = set()
    for uid in list(sessions.keys()) + list(user_config.keys()):
        known_users.add(uid)
    if known_users:
        token = session["token"]
        base_url = session["baseUrl"]
        print(f"[START] 通知 {len(known_users)} 位历史用户 bridge 已上线...")
        for uid in known_users:
            try:
                send_message(base_url, token, uid,
                             "[Claude Code] bridge 已上线，服务已恢复。")
                print(f"   [START] 已通知 {uid}")
            except Exception as e:
                print(f"   [WARN] 通知 {uid} 失败: {e}")

    main_loop(session, sessions, user_config)
