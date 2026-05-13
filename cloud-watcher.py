"""云版定时任务守护 — 部署到 Railway，24小时运行"""
import json
import time
import urllib.request
import os
import http.server
import threading

# === 配置 ===
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "e6e39641de514a96923d520a57ee36b9")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/29b0e944-7428-41df-bafa-40bdafa16c38")
CLOUD_SECRET = os.environ.get("CLOUD_SECRET", "crm-sync-2026")

DATA_FILE = "/tmp/crm-data.json"
CHECK_INTERVAL = 60
REMINDER_INTERVAL = 3600

def push_feishu(text):
    try:
        data = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("code") == 0
    except Exception:
        return False

def push_wechat(title, content):
    try:
        data = json.dumps({"token": PUSHPLUS_TOKEN, "title": title, "content": content}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request("https://www.pushplus.plus/send", data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("code") == 200
    except Exception:
        return False

def push_all(text, title=""):
    f_ok = push_feishu(text)
    w_ok = push_wechat(title or "提醒", text)
    return f_ok or w_ok

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def check_tasks():
    data = load_data()
    if not data:
        return

    tasks = data.get("tasks", [])
    if not tasks:
        return

    now = time.localtime()
    today = time.strftime("%Y-%m-%d")
    current_time = time.strftime("%H:%M")
    weekday = now.tm_wday

    pushed = False
    for t in tasks:
        if not t.get("enabled", True):
            continue
        if t.get("time", "") != current_time:
            continue
        if t.get("lastSent") == today:
            continue

        repeat = t.get("repeat", "once")
        should_fire = False
        if repeat == "daily":
            should_fire = True
        elif repeat == "weekly":
            if weekday in t.get("days", []):
                should_fire = True
        elif repeat == "once":
            if t.get("targetDate", "") == today:
                should_fire = True

        if should_fire:
            title = t.get("title", "定时提醒")
            text = f"⏰ {title}"
            if t.get("content"):
                text += f"\n{t['content']}"
            if push_all(text, title):
                t["lastSent"] = today
                pushed = True

    if pushed:
        save_data(data)

def check_reminders():
    data = load_data()
    if not data:
        return

    customers = data.get("customers", [])
    if not customers:
        return

    today = time.strftime("%Y-%m-%d")
    settings = data.get("settings", {})
    if settings.get("lastPushDate") == today:
        return

    overdue_fu, today_fu, overdue_orders = [], [], []
    for c in customers:
        if c.get("status") == "待跟进" and c.get("nextFollowup"):
            nf = c["nextFollowup"]
            if nf < today:
                overdue_fu.append(f"⚠ {c['name']} — 跟进已过期 ({nf})")
            elif nf == today:
                today_fu.append(f"⏰ {c['name']} — 今天需要跟进")
        for o in c.get("orders", []):
            if o.get("deliveryDate") and o["deliveryDate"] < today and o.get("progress") not in ("已发货", "已流失"):
                overdue_orders.append(f"🚨 {c['name']} — 订单「{o['product']}」交期已过 ({o['deliveryDate']})，当前: {o.get('progress','未知')}")

    if not (overdue_fu or today_fu or overdue_orders):
        return

    parts = []
    if overdue_fu:
        parts.append("【已过期跟进】\n" + "\n".join(overdue_fu))
    if today_fu:
        parts.append("【今日跟进】\n" + "\n".join(today_fu))
    if overdue_orders:
        parts.append("【订单交期逾期】\n" + "\n".join(overdue_orders))

    text = "\n\n".join(parts)
    if push_all(text, "客户管理提醒"):
        settings["lastPushDate"] = today
        save_data(data)

def scheduler():
    reminder_countdown = 0
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            check_tasks()
        except Exception:
            pass
        reminder_countdown += CHECK_INTERVAL
        if reminder_countdown >= REMINDER_INTERVAL:
            reminder_countdown = 0
            try:
                check_reminders()
            except Exception:
                pass

# HTTP 服务：健康检查 + 数据同步接收
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            data = load_data()
            last_sync = "never"
            if data:
                settings = data.get("settings", {})
                last_sync = settings.get("lastSyncTime", "unknown")
            self.wfile.write(json.dumps({"status": "ok", "lastSync": last_sync}).encode())
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"CRM Cloud Watcher Running")

    def do_POST(self):
        if self.path == "/sync":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body)
                secret = payload.get("secret", "")
                if secret != CLOUD_SECRET:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"Forbidden")
                    return
                sync_data = payload.get("data", {})
                settings = sync_data.get("settings", {})
                settings["lastSyncTime"] = time.strftime("%Y-%m-%d %H:%M:%S")
                sync_data["settings"] = settings
                save_data(sync_data)
                print(f"数据已同步: {len(sync_data.get('customers',[]))} 客户, {len(sync_data.get('tasks',[]))} 任务")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode())
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, *args):
        pass  # 禁用 HTTP 日志

def start_server():
    port = int(os.environ.get("PORT", 8080))
    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    print(f"云端守护进程启动 — 端口: {port}")
    server.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    scheduler()
