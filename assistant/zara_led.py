#!/usr/bin/env python3
"""Answers the LED-matrix sketch over arduino-router (msgpack-RPC on a unix socket).

Provides:
  zara_status() -> int   0 off (quiet hours) | 1 ok | 2 a zara service is down | 3 critical, unacknowledged
                         | 10+n  n unread Mattermost DMs/mentions | 10000+HHMM idle clock | 20000+HHMM dim clock (quiet hours)
  zara_date()   -> int   weekday*10000 + day*100 + month     zara_temp() -> int  outdoor °C (-999 unknown)
  zara_ack()    -> bool  acknowledge: stop call retries, clear "critical", note it in the alerts channel
"""
import math
import json, os, socket, subprocess, sys, threading, time, urllib.parse, urllib.request
import msgpack

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alert_triage import QUIET_TZ, quiet_now  # noqa: E402  (same quiet-hours rule as the calls)
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

SOCK = "/var/run/arduino-router.sock"
DECISIONS = os.path.expanduser("~/triage/decisions.jsonl")
ACK_FILE = os.path.expanduser("~/triage/.led_ack")
CRITICAL_WINDOW = 30 * 60
SERVICES = ["zara-assistant", "mm-forwarder", "alert-triage", "tgcaller", "prometheus"]
TG = "https://api.telegram.org/bot%s/" % os.environ["MM_TG_BOT_TOKEN"]
ALERT_CHAT = os.environ["MM_TG_ALERT_CHAT_ID"]

_svc_cache = (0.0, True)


def log(*a):
    print(*a, flush=True)


def services_ok():
    global _svc_cache
    if time.time() - _svc_cache[0] > 30:
        r = subprocess.run(["systemctl", "is-active", *SERVICES], capture_output=True, text=True)
        _svc_cache = (time.time(), all(l.strip() == "active" for l in r.stdout.splitlines()))
    return _svc_cache[1]


def ack_time():
    try:
        return float(open(ACK_FILE).read())
    except Exception:
        return 0.0


def critical():
    since = max(time.time() - CRITICAL_WINDOW, ack_time())
    try:
        with open(DECISIONS, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            tail = f.read().decode(errors="ignore").splitlines()[1:]
    except FileNotFoundError:
        return False
    for l in reversed(tail):
        try:
            d = json.loads(l)
        except ValueError:
            continue
        if d["t"] < since:
            break
        if d.get("call") and not d.get("quiet"):
            return True
    return False


MM, MM_TOKEN = os.environ.get("MM_URL", ""), os.environ.get("MM_TOKEN", "")
unread = {"n": 0, "ok": False}


def poll_mattermost():
    """Every 30 s: unread mentions/DMs the way the Mattermost UI counts them with collapsed reply threads:
    root-post mentions per channel (mention_count_root) + unread mentions in followed threads per team.
    (Plain mention_count also includes thread replies and stays high after the threads are read.)"""
    def get(path):
        req = urllib.request.Request(MM + path, headers={"Authorization": "Bearer " + MM_TOKEN})
        return json.load(urllib.request.urlopen(req, timeout=20))

    while True:
        try:
            total, page = 0, 0
            while True:
                members = get("/api/v4/users/me/channel_members?page=%d&per_page=200" % page)
                total += sum(m.get("mention_count_root", m.get("mention_count", 0)) for m in members)
                if len(members) < 200:
                    break
                page += 1
            for team in get("/api/v4/users/me/teams"):
                t = get("/api/v4/users/me/teams/%s/threads?totalsOnly=true&threadsOnly=false" % team["id"])
                total += t.get("total_unread_mentions", 0)
            if total != unread["n"]:
                log("unread Mattermost mentions/DMs:", total)
            unread.update(n=total, ok=True)
        except Exception as e:
            log("mattermost poll failed:", repr(e))
        time.sleep(30)


def zara_status():
    if critical():
        return 3
    now = datetime.now(ZoneInfo(QUIET_TZ))
    if quiet_now():
        return 20000 + now.hour * 100 + now.minute    # night: dim clock banner only
    if not services_ok():
        return 2
    if unread["n"] > 0:
        return 10 + min(unread["n"], 99)
    return 10000 + now.hour * 100 + now.minute    # idle: show the clock


WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&current=temperature_2m"
               % (os.environ.get("WEATHER_LAT", "60.45"), os.environ.get("WEATHER_LON", "22.27")))  # Turku
weather = {"t": None, "at": 0.0}


def poll_weather():
    """Every 15 min: outdoor temperature from Open-Meteo (no key needed)."""
    while True:
        try:
            weather.update(t=json.load(urllib.request.urlopen(WEATHER_URL, timeout=20))["current"]["temperature_2m"],
                           at=time.time())
        except Exception as e:
            log("weather poll failed:", repr(e))
        time.sleep(15 * 60)


def zara_date():
    """weekday (0=Mon) * 10000 + day * 100 + month, local time."""
    now = datetime.now(ZoneInfo(QUIET_TZ))
    return now.weekday() * 10000 + now.day * 100 + now.month


def zara_temp():
    """Outdoor temperature in whole °C, or -999 if unknown / older than 1 h."""
    if weather["t"] is None or time.time() - weather["at"] > 3600:
        return -999
    return math.floor(weather["t"] + 0.5)   # half up, same as JS Math.round on the web page


def zara_ack():
    open(ACK_FILE, "w").write(str(time.time()))
    try:
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8099/ack", data=b"", method="POST"),
                               timeout=3).read()
    except Exception as e:
        log("caller ack failed:", repr(e))
    try:
        data = urllib.parse.urlencode({"chat_id": ALERT_CHAT,
                                       "text": "✅ Acknowledged with zara's button — call retries stopped."}).encode()
        urllib.request.urlopen(TG + "sendMessage", data=data, timeout=10).read()
    except Exception as e:
        log("telegram ack note failed:", repr(e))
    log("acknowledged via button")
    return True


HANDLERS = {"zara_status": zara_status, "zara_ack": zara_ack, "zara_date": zara_date, "zara_temp": zara_temp}


def serve():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    lock = threading.Lock()

    def send(obj):
        with lock:
            s.sendall(msgpack.packb(obj, use_bin_type=True))

    for i, name in enumerate(HANDLERS, start=1):
        send([0, i, "$/register", [name]])
    log("connected to arduino-router; providing", ", ".join(HANDLERS))
    unpacker = msgpack.Unpacker(raw=False)
    while True:
        chunk = s.recv(65536)
        if not chunk:
            raise ConnectionError("router closed the connection")
        unpacker.feed(chunk)
        for msg in unpacker:
            if not isinstance(msg, list) or not msg:
                continue
            if msg[0] == 0:                       # request: [0, id, method, params]
                _, msgid, method, params = msg
                fn = HANDLERS.get(method)
                try:
                    send([1, msgid, None, fn(*params)] if fn else [1, msgid, [1, "no such method"], None])
                except Exception as e:
                    send([1, msgid, [1, repr(e)], None])
            elif msg[0] == 1 and msg[2]:          # response with error (e.g. registration)
                log("router error:", msg[2])


if __name__ == "__main__":
    if MM and MM_TOKEN:
        threading.Thread(target=poll_mattermost, daemon=True).start()
    threading.Thread(target=poll_weather, daemon=True).start()
    while True:
        try:
            serve()
        except Exception as e:
            log("router connection lost:", repr(e), "- retrying in 5 s")
            time.sleep(5)
