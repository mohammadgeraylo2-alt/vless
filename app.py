#!/usr/bin/env python3
"""
یه فایل به‌جای سه تا سرور همه‌کاره Xray روی Railway:
    python3 app.py render        -> ساخت config.json زنده از قالب clients.json
    python3 app.py manage        -> اجرای API مدیریتی (پورت 8081)
    python3 app.py quota-check   -> یه بار چک حجم/انقضا و حذف کاربرای منقضی‌شده
"""
import html
import json
import os
import subprocess
import sys
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

STATE_DIR = os.environ.get("XRAY_STATE_DIR", "/data")
CLIENTS_PATH = os.path.join(STATE_DIR, "clients.json")
USAGE_PATH = os.path.join(STATE_DIR, "usage_totals.json")
LIVE_CONFIG_PATH = "/etc/xray/config.json"
FLAG_PATH = "/tmp/config_changed"
MANAGE_SECRET = os.environ.get("MANAGE_SECRET", "")
GB = 1024 ** 3

# ---- Reality settings (replace the old PUBLIC_DOMAIN / WS_PATH pair) -----
# داخلی: پورتی که خود Xray روش گوش می‌ده (باید همون پورتی باشه که در
# Railway -> Settings -> Networking -> TCP Proxy به عنوان "Target Port" دادی)
REALITY_LISTEN_PORT = int(os.environ.get("REALITY_LISTEN_PORT", "443"))

# دست‌دهی TLS واقعی به جای این دامنه انجام می‌شه (باید یه سایت واقعی و
# پشتیبان TLS1.3 + HTTP/2 باشه، مثلا یه سایت مایکروسافت/سرویس ابری معروف)
REALITY_DEST = os.environ.get("REALITY_DEST", "www.microsoft.com:443")
REALITY_SERVER_NAMES = [
    s.strip() for s in os.environ.get("REALITY_SERVER_NAMES", "www.microsoft.com").split(",") if s.strip()
]
REALITY_PRIVATE_KEY = os.environ.get("REALITY_PRIVATE_KEY", "")
REALITY_PUBLIC_KEY = os.environ.get("REALITY_PUBLIC_KEY", "")
_raw_short_ids = os.environ.get("REALITY_SHORT_IDS", "").strip()
REALITY_SHORT_IDS = [s.strip() for s in _raw_short_ids.split(",")] if _raw_short_ids else [""]

# بیرونی: هاست:پورتی که ریلوی از طریق TCP Proxy بهت داده
# (مثلا shuttle.proxy.rlwy.net) و پورت جداگونه‌ش
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")
PUBLIC_PORT = os.environ.get("PUBLIC_PORT", "443")

# دامنه‌ی HTTP عمومی سرویس (همون دامنه‌ای که ریلوی زیر Networking -> Public
# Networking برای پورت 8081 ساخته، مثلا instageam-production-xxxx.up.railway.app)
# این برای صفحه‌ی عمومی چک حجم/باقی‌مونده استفاده می‌شه، نه برای خودِ Xray.
STATUS_DOMAIN = os.environ.get("PUBLIC_DOMAIN", "").strip()

CONFIG_TEMPLATE = {
    "log": {"loglevel": "warning"},
    "api": {"tag": "api", "services": ["HandlerService", "LoggerService", "StatsService"]},
    "stats": {},
    "policy": {
        "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
        "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
    },
    "inbounds": [
        {
            "listen": "127.0.0.1",
            "port": 10085,
            "protocol": "dokodemo-door",
            "settings": {"address": "127.0.0.1"},
            "tag": "api",
        },
        {
            "listen": "0.0.0.0",
            "port": REALITY_LISTEN_PORT,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": REALITY_DEST,
                    "xver": 0,
                    "serverNames": REALITY_SERVER_NAMES,
                    "privateKey": REALITY_PRIVATE_KEY,
                    "shortIds": REALITY_SHORT_IDS,
                },
            },
        },
    ],
    "outbounds": [
        {"protocol": "freedom", "tag": "direct"},
        {"protocol": "blackhole", "tag": "blocked"},
    ],
    "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]},
}


def _require_reality_env():
    missing = [
        name for name, val in [
            ("REALITY_PRIVATE_KEY", REALITY_PRIVATE_KEY),
            ("REALITY_PUBLIC_KEY", REALITY_PUBLIC_KEY),
            ("PUBLIC_HOST", PUBLIC_HOST),
        ] if not val
    ]
    if missing:
        print(f"[config] هشدار: این متغیرها ست نشدن: {', '.join(missing)}")


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# --------------------------------------------------------------------------
def render_config():
    clients = load_json(CLIENTS_PATH, [])
    config = json.loads(json.dumps(CONFIG_TEMPLATE))
    for inbound in config["inbounds"]:
        if inbound.get("protocol") == "vless":
            inbound["settings"]["clients"] = [
                {
                    "id": c["uuid"],
                    "level": 0,
                    "email": c["email"],
                    "flow": "xtls-rprx-vision",
                }
                for c in clients
            ]
    save_json(LIVE_CONFIG_PATH, config)
    print(f"[render] wrote {LIVE_CONFIG_PATH} with {len(clients)} client(s)")


def flag_restart():
    with open(FLAG_PATH, "w") as f:
        f.write("1")


def build_vless_link(client_uuid, gb, days):
    if not (PUBLIC_HOST and REALITY_PUBLIC_KEY):
        return None
    sni = REALITY_SERVER_NAMES[0] if REALITY_SERVER_NAMES else REALITY_DEST.split(":")[0]
    sid = REALITY_SHORT_IDS[0]
    return (
        f"vless://{client_uuid}@{PUBLIC_HOST}:{PUBLIC_PORT}"
        f"?type=tcp&security=reality&flow=xtls-rprx-vision"
        f"&pbk={REALITY_PUBLIC_KEY}&fp=chrome&sni={sni}&sid={sid}&spx=%2F"
        f"#Relay-{gb}GB-{days}d"
    )


def build_status_link(client_uuid):
    """لینک صفحه‌ی عمومیِ چک حجم/انقضا برای این کاربر (بدون نیاز به رمز)."""
    if not STATUS_DOMAIN:
        return None
    return f"https://{STATUS_DOMAIN}/check?uuid={client_uuid}"


# --------------------------------------------------------------------------
def get_stats():
    try:
        out = subprocess.check_output(
            ["xray", "api", "statsquery", "--server=127.0.0.1:10085"],
            stderr=subprocess.DEVNULL,
        )
        return json.loads(out)
    except Exception as e:
        print(f"[usage] error querying stats API: {e}")
        return None


def get_raw_usage():
    stats = get_stats()
    raw_usage = {}
    if stats:
        for stat in stats.get("stat", []):
            parts = stat["name"].split(">>>")
            if len(parts) == 4 and parts[0] == "user":
                raw_usage[parts[1]] = raw_usage.get(parts[1], 0) + int(stat.get("value", 0))
    return raw_usage


def build_usage_report():
    clients = load_json(CLIENTS_PATH, [])
    if not clients:
        return []

    raw_usage = get_raw_usage()
    usage_state = load_json(USAGE_PATH, {})
    today = datetime.now(timezone.utc).date().isoformat()

    report = []
    for client in clients:
        email = client["email"]
        raw = raw_usage.get(email, 0)
        state = usage_state.get(email, {"total": 0, "last_raw": 0})
        delta = raw - state["last_raw"] if raw >= state["last_raw"] else raw
        used_bytes = state["total"] + delta
        total_bytes = client["gb"] * GB
        remaining_bytes = max(total_bytes - used_bytes, 0)

        report.append({
            "email": email,
            "uuid": client["uuid"],
            "gb": client["gb"],
            "days_expires": client["expires"],
            "expired": today > client["expires"],
            "used_gb": round(used_bytes / GB, 3),
            "remaining_gb": round(remaining_bytes / GB, 3),
        })
    return report


def build_single_status(client_uuid):
    """گزارش وضعیت یه کاربر خاص (برای صفحه‌ی عمومی چک حجم)."""
    clients = load_json(CLIENTS_PATH, [])
    client = next((c for c in clients if c["uuid"] == client_uuid), None)
    if not client:
        return None

    raw_usage = get_raw_usage()
    usage_state = load_json(USAGE_PATH, {})
    today = datetime.now(timezone.utc).date().isoformat()

    email = client["email"]
    raw = raw_usage.get(email, 0)
    state = usage_state.get(email, {"total": 0, "last_raw": 0})
    delta = raw - state["last_raw"] if raw >= state["last_raw"] else raw
    used_bytes = state["total"] + delta
    total_bytes = client["gb"] * GB
    remaining_bytes = max(total_bytes - used_bytes, 0)

    return {
        "gb": client["gb"],
        "expires": client["expires"],
        "expired": today > client["expires"],
        "used_gb": round(used_bytes / GB, 3),
        "remaining_gb": round(remaining_bytes / GB, 3),
    }


def revoke_client(uuid_prefix):
    """کاربری که uuid‌ش با uuid_prefix شروع می‌شه رو حذف می‌کنه. برمی‌گردونه: (موفق؟, ایمیل حذف‌شده یا پیام خطا)"""
    clients = load_json(CLIENTS_PATH, [])
    matches = [c for c in clients if c["uuid"].startswith(uuid_prefix)]

    if not matches:
        return False, "کاربری با این UUID پیدا نشد"
    if len(matches) > 1:
        return False, "چند کاربر با این پیشوند مطابقت دارن، UUID کامل‌تری بده"

    target = matches[0]
    remaining = [c for c in clients if c["uuid"] != target["uuid"]]
    save_json(CLIENTS_PATH, remaining)
    render_config()
    flag_restart()
    return True, target["email"]


# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_payload(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _check_secret(self, payload):
        return MANAGE_SECRET and payload.get("secret") == MANAGE_SECRET

    def do_POST(self):
        if self.path == "/create":
            self._handle_create()
        elif self.path == "/usage":
            self._handle_usage()
        elif self.path == "/revoke":
            self._handle_revoke()
        else:
            self._send(404, {"error": "not found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/check":
            self._handle_check(parse_qs(parsed.query))
        else:
            self._send(404, {"error": "not found"})

    def _send_html(self, status, body):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _handle_check(self, query):
        client_uuid = (query.get("uuid") or [""])[0].strip()
        page = (
            "<!doctype html><html lang='fa' dir='rtl'>"
            "<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>وضعیت اکانت</title>"
            "<style>body{font-family:sans-serif;background:#111;color:#eee;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
            ".card{background:#1c1c1c;padding:24px 32px;border-radius:12px;max-width:360px;text-align:center}"
            "h2{margin-top:0}.bar{background:#333;border-radius:8px;height:14px;overflow:hidden;margin:12px 0}"
            ".fill{background:#7c5cff;height:100%}.err{color:#ff6b6b}</style><div class='card'>"
        )
        if not client_uuid:
            page += "<h2 class='err'>UUID داده نشده</h2></div></html>"
            self._send_html(400, page)
            return

        status = build_single_status(client_uuid)
        if not status:
            page += "<h2 class='err'>کاربری با این UUID پیدا نشد (شاید حذف یا منقضی شده)</h2></div></html>"
            self._send_html(404, page)
            return

        pct_used = 0
        if status["gb"] > 0:
            pct_used = min(100, round((status["used_gb"] / status["gb"]) * 100))
        state_label = "منقضی‌شده" if status["expired"] else "فعال"
        state_class = "err" if status["expired"] else ""
        page += (
            f"<h2>وضعیت اکانت</h2>"
            f"<p class='{state_class}'>{html.escape(state_label)}</p>"
            f"<div class='bar'><div class='fill' style='width:{pct_used}%'></div></div>"
            f"<p>{status['used_gb']} / {status['gb']} گیگ مصرف‌شده</p>"
            f"<p>{status['remaining_gb']} گیگ باقی‌مونده</p>"
            f"<p>انقضا: {html.escape(status['expires'])}</p>"
            f"</div></html>"
        )
        self._send_html(200, page)

    def _handle_create(self):
        try:
            payload = self._read_payload()
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return
        if not self._check_secret(payload):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            gb = int(payload["gb"])
            days = int(payload["days"])
            assert gb > 0 and days > 0
        except (KeyError, ValueError, AssertionError):
            self._send(400, {"error": "gb and days must be positive integers"})
            return

        client_uuid = str(uuidlib.uuid4())
        email = f"u_{client_uuid[:8]}"
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()

        clients = load_json(CLIENTS_PATH, [])
        clients.append({
            "uuid": client_uuid, "email": email, "gb": gb,
            "created": datetime.now(timezone.utc).date().isoformat(),
            "expires": expires,
        })
        save_json(CLIENTS_PATH, clients)
        render_config()
        flag_restart()

        self._send(200, {
            "uuid": client_uuid, "email": email, "gb": gb, "days": days,
            "expires": expires, "vless_link": build_vless_link(client_uuid, gb, days),
            "status_link": build_status_link(client_uuid),
        })

    def _handle_usage(self):
        try:
            payload = self._read_payload()
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return
        if not self._check_secret(payload):
            self._send(401, {"error": "unauthorized"})
            return

        report = build_usage_report()
        self._send(200, {"clients": report})

    def _handle_revoke(self):
        try:
            payload = self._read_payload()
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return
        if not self._check_secret(payload):
            self._send(401, {"error": "unauthorized"})
            return

        uuid_prefix = payload.get("uuid", "")
        if not uuid_prefix:
            self._send(400, {"error": "uuid is required"})
            return

        ok, result = revoke_client(uuid_prefix)
        if ok:
            self._send(200, {"revoked": True, "email": result})
        else:
            self._send(404, {"error": result})

    def log_message(self, fmt, *args):
        print("[manage]", fmt % args)


def run_manage_api():
    port = int(os.environ.get("MANAGE_PORT", 8081))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[manage] listening on :{port}")
    server.serve_forever()


# --------------------------------------------------------------------------
def quota_check():
    clients = load_json(CLIENTS_PATH, [])
    if not clients:
        return

    raw_usage = get_raw_usage()
    usage_state = load_json(USAGE_PATH, {})
    today = datetime.now(timezone.utc).date().isoformat()

    keep, removed = [], []
    for client in clients:
        email = client["email"]
        raw = raw_usage.get(email, 0)
        state = usage_state.get(email, {"total": 0, "last_raw": 0})
        delta = raw - state["last_raw"] if raw >= state["last_raw"] else raw
        state["total"] += delta
        state["last_raw"] = raw
        usage_state[email] = state

        expired = today > client["expires"]
        over_quota = state["total"] >= client["gb"] * GB
        if expired or over_quota:
            reason = "expired" if expired else "over quota"
            print(f"[quota-check] removing {email} ({reason}, used {state['total']/GB:.2f} GB)")
            removed.append(email)
        else:
            keep.append(client)

    save_json(USAGE_PATH, usage_state)
    if removed:
        save_json(CLIENTS_PATH, keep)
        render_config()
        flag_restart()


# --------------------------------------------------------------------------
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    os.makedirs(STATE_DIR, exist_ok=True)
    _require_reality_env()
    if cmd == "render":
        render_config()
    elif cmd == "manage":
        run_manage_api()
    elif cmd == "quota-check":
        quota_check()
    else:
        print("usage: app.py [render|manage|quota-check]")
        sys.exit(1)
                                                                       
