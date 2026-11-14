#!/usr/bin/env python3
"""Telegram <-> Claude Code bridge. Only answers TELEGRAM_ALLOWED_USER."""
import json, os, queue, signal, subprocess, threading, time, urllib.error, urllib.request, urllib.parse
import live
import stats
import tgmd

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED = int(os.environ["TELEGRAM_ALLOWED_USER"])
API = f"https://api.telegram.org/bot{TOKEN}/"
CLAUDE = os.path.expanduser("~/.local/bin/claude")
WORKDIR = os.path.expanduser("~/assistant")
STATE = os.path.join(WORKDIR, ".session")
USAGE_CACHE = os.path.join(WORKDIR, ".usage.json")
PROBE_DIR = os.path.expanduser("~/.usage-probe")
WIDGET_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_widget.html")
STATS_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats_widget.html")
TIMEOUT = 600
USAGE_MAX_AGE = 300
WINDOW_NAMES = {"five_hour": "Session (5h)", "seven_day": "Weekly", "seven_day_opus": "Weekly · Opus",
                "seven_day_sonnet": "Weekly · Sonnet"}


def tg(method, **params):
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list, bool)) else v)
                                   for k, v in params.items()}).encode()
    with urllib.request.urlopen(API + method, data=data, timeout=70) as r:
        return json.load(r)


def send(chat_id, text):
    text = text.strip() or "(no output)"
    for part in tgmd.chunks(text):
        try:
            tg("sendMessage", chat_id=chat_id, text=tgmd.to_html(part), parse_mode="HTML",
               disable_web_page_preview="true")
        except urllib.error.HTTPError:  # formatting rejected -> plain text
            tg("sendMessage", chat_id=chat_id, text=part)


def load_session():
    try:
        return open(STATE).read().strip() or None
    except FileNotFoundError:
        return None


def save_session(sid):
    with open(STATE, "w") as f:
        f.write(sid or "")


def keep_typing(chat_id, stop):
    while not stop.is_set():
        try:
            tg("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass
        stop.wait(4)


# ---------- usage ----------
def save_usage(info):
    try:
        with open(USAGE_CACHE, "w") as f:
            json.dump({"t": time.time(), "info": info}, f)
    except Exception:
        pass


current = {"proc": None, "cancelled": False, "active": False, "draft_id": None}
jobs: "queue.Queue[dict]" = queue.Queue()


def kill_group(p):
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 2)):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            return
        try:
            p.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_stream(cmd, cwd, track=False, on_event=None):
    """Run claude with stream-json, reading events as they arrive.
    Returns (final result event or None, error tail).
    track=True registers the process so /cancel can kill it (and everything it started).
    on_event, if given, is called with every event (live progress)."""
    extra = ["--output-format", "stream-json", "--verbose"]
    if on_event:
        extra.append("--include-partial-messages")
    p = subprocess.Popen(cmd + extra, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, bufsize=1, start_new_session=True)
    if track:
        current["proc"] = p
        if current["cancelled"]:  # /cancel arrived while we were starting up
            kill_group(p)
    err_buf = []
    threading.Thread(target=lambda: err_buf.append(p.stderr.read()), daemon=True).start()
    timed_out = threading.Event()
    timer = threading.Timer(TIMEOUT, lambda: (timed_out.set(), kill_group(p)))
    timer.start()
    final, tail = None, []
    try:
        for line in p.stdout:
            tail = (tail + [line])[-5:]
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "rate_limit_event":
                save_usage(ev.get("rate_limit_info") or {})
            elif ev.get("type") == "result":
                final = ev
            if on_event:
                try:
                    on_event(ev)
                except Exception as e:
                    print("live view error:", repr(e), flush=True)
        p.wait()
    finally:
        timer.cancel()
        if track:
            current["proc"] = None
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, TIMEOUT)
    return final, ("".join(err_buf) or "".join(tail))[-1500:]


def probe_usage():
    os.makedirs(PROBE_DIR, exist_ok=True)
    run_stream([CLAUDE, "-p", "Reply with: ok", "--model", "haiku", "--max-turns", "1",
                "--strict-mcp-config"], PROBE_DIR)


def get_usage(force=False):
    try:
        c = json.load(open(USAGE_CACHE))
    except Exception:
        c = None
    if force or not c or time.time() - c["t"] > USAGE_MAX_AGE:
        probe_usage()
        try:
            c = json.load(open(USAGE_CACHE))
        except Exception:
            return None
    return c


def usage_payload(c):
    info = c["info"]
    wins = info.get("unifiedWindows") or {}
    rows = [[WINDOW_NAMES.get(k, k.replace("_", " ")), round(v.get("utilization", 0), 3),
             v.get("resetsAt", 0), v.get("status", "")] for k, v in wins.items()]
    order = list(WINDOW_NAMES)
    rows.sort(key=lambda r: next((i for i, k in enumerate(order) if WINDOW_NAMES[k] == r[0]), 99))
    data = {"w": rows, "s": info.get("status", "?"), "t": int(c["t"])}
    caption = " · ".join("%s %d%%" % (r[0], round(r[1] * 100)) for r in rows) or "No usage data"
    return json.dumps(data, separators=(",", ":")), caption


def cmd_usage(chat_id):
    c = get_usage()
    if not c:
        return send(chat_id, "Couldn't read usage right now.")
    data, caption = usage_payload(c)
    try:
        tg("sendWidget", chat_id=chat_id, html=open(WIDGET_HTML).read(), chat_data=data,
           caption=caption, allow_queries=True)
    except urllib.error.HTTPError as e:
        print("sendWidget failed:", e.read().decode()[:300], flush=True)
        send(chat_id, "**Claude usage**\n" + caption.replace(" · ", "\n"))


def cmd_stats(chat_id):
    try:
        data, caption = stats.collect("6h")
    except Exception as e:
        return send(chat_id, f"Couldn't read stats from Prometheus: {e}")
    try:
        tg("sendWidget", chat_id=chat_id, html=open(STATS_HTML).read(), chat_data=data,
           caption=caption, allow_queries=True)
    except urllib.error.HTTPError as e:
        print("sendWidget (stats) failed:", e.read().decode()[:300], flush=True)
        send(chat_id, caption)


def on_widget_query(q):
    try:
        tg("answerWidgetQuery", widget_query_id=q["id"], data="refreshing")
    except Exception as e:
        print("answerWidgetQuery failed:", e, flush=True)
    if q.get("from", {}).get("id") != ALLOWED or not q.get("chat") or not q.get("message_id"):
        return
    qd = q.get("data", "")
    if qd.startswith("s:"):
        try:
            data, caption = stats.collect(qd[2:])
        except Exception as e:
            print("stats refresh failed:", e, flush=True)
            return
    elif qd == "refresh":
        c = get_usage(force=True)
        if not c:
            return
        data, caption = usage_payload(c)
    else:
        return
    for method, field in (("editMessageWidgetData", {"chat_data": data}),
                          ("editMessageCaption", {"caption": caption})):
        try:
            tg(method, chat_id=q["chat"]["id"], message_id=q["message_id"], **field)
        except urllib.error.HTTPError as e:
            msg = e.read().decode()
            if "not modified" not in msg:
                print("widget update failed:", msg[:300], flush=True)


# ---------- conversation ----------
def ask_claude(prompt, on_event=None):
    cmd = [CLAUDE, "-p", prompt, "--dangerously-skip-permissions"]
    sid = load_session()
    if sid:
        cmd += ["--resume", sid]
    final, err = run_stream(cmd, WORKDIR, track=True, on_event=on_event)
    if current["cancelled"]:
        return None
    if final is None:
        if sid:  # session may be gone; retry fresh once
            save_session(None)
            return ask_claude(prompt, on_event)
        return f"Claude error:\n{err}"
    save_session(final.get("session_id"))
    return final.get("result") or "(empty reply)"


HELP = ("Hi, I'm zara. Just talk to me.\n"
        "/new — start a fresh conversation\n"
        "/cancel — stop what I'm doing right now\n"
        "/usage — Claude plan usage\n"
        "/stats — zara resource usage (CPU, RAM, disk, temp, network)\n"
        "/briefing — morning briefing now (also sent daily at 08:00)")


def busy():
    return current["active"] or not jobs.empty()


def worker():
    """Runs Claude requests one at a time so the poll loop stays free for /cancel."""
    while True:
        chat_id, text = jobs.get()
        current["cancelled"] = False
        current["active"] = True
        view = live.LiveView(tg, chat_id).start()
        current["draft_id"] = view.draft_id
        try:
            reply = ask_claude(text, on_event=view.on_event)
        except subprocess.TimeoutExpired:
            reply = "That took too long, so I stopped. Try a smaller request."
        except Exception as e:
            reply = f"Error: {e}"
        finally:
            view.stop()
            current["active"] = False
            current["draft_id"] = None
        if reply is not None and not current["cancelled"]:
            send_final(chat_id, reply, view.summary())


def send_final(chat_id, reply, summary=""):
    """Native rich Markdown (tables, task lists, collapsible steps); falls back to converted HTML."""
    md = (reply.strip() or "(no output)") + summary
    if len(md) < 30000:
        try:
            tg("sendRichMessage", chat_id=chat_id, rich_message={"markdown": md})
            return
        except urllib.error.HTTPError as e:
            print("sendRichMessage failed, falling back:", e.read().decode()[:200], flush=True)
    send(chat_id, reply)


def cancel(chat_id):
    dropped = 0
    while not jobs.empty():
        try:
            jobs.get_nowait()
            dropped += 1
        except queue.Empty:
            break
    p = current["proc"]
    if not current["active"] and not dropped:
        return send(chat_id, "Nothing to cancel.")
    current["cancelled"] = True
    if p is not None:
        kill_group(p)
    send(chat_id, "⏹ Stopped." + (" Dropped %d queued message(s)." % dropped if dropped else "")
         + " The conversation is kept — carry on or rephrase.")


def handle(msg):
    """Runs on the poll loop: commands are instant, everything else goes to the worker."""
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")
    cmd = text.split("@")[0].strip() if text.startswith("/") else ""
    if cmd in ("/start", "/help"):
        return tg("sendMessage", chat_id=chat_id, text=HELP, reply_markup={"remove_keyboard": True})
    if cmd == "/cancel":
        return cancel(chat_id)
    if cmd == "/new":
        if busy():
            return send(chat_id, "I'm busy — /cancel first, then /new.")
        save_session(None)
        return send(chat_id, "Started a fresh conversation.")
    if cmd == "/usage":
        return threading.Thread(target=cmd_usage, args=(chat_id,), daemon=True).start()
    if cmd == "/stats":
        return threading.Thread(target=cmd_stats, args=(chat_id,), daemon=True).start()
    if cmd == "/briefing":
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "briefing.py")
        send(chat_id, "☀️ Putting your briefing together — about a minute.")
        return threading.Thread(target=subprocess.run, args=(["/usr/bin/python3", script],),
                                kwargs={"timeout": 900}, daemon=True).start()
    if not text:
        return send(chat_id, "I can only read text for now.")
    if busy():
        send(chat_id, "⏳ Queued — I'll get to it after the current request. /cancel to stop.")
    jobs.put((chat_id, text))


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    try:
        tg("setMyCommands", commands=[{"command": "new", "description": "Start a fresh conversation"},
                                      {"command": "cancel", "description": "Stop the current request"},
                                      {"command": "usage", "description": "Claude plan usage"},
                                      {"command": "stats", "description": "zara resource usage"},
                                      {"command": "briefing", "description": "Morning briefing now"}])
    except Exception:
        pass
    threading.Thread(target=worker, daemon=True).start()
    offset = None
    print("zara assistant started", flush=True)
    while True:
        try:
            params = {"timeout": 60,
                      "allowed_updates": ["message", "widget_query", "stopped_message_generation"]}
            if offset:
                params["offset"] = offset
            for u in tg("getUpdates", **params).get("result", []):
                offset = u["update_id"] + 1
                st = u.get("stopped_message_generation")
                if st:  # Stop button under the live draft == /cancel
                    if st.get("draft_id") == current["draft_id"] and current["active"]:
                        cancel(st["chat"]["id"])
                    continue
                if "widget_query" in u:  # must be answered within 15 s, so never block on a Claude call
                    threading.Thread(target=on_widget_query, args=(u["widget_query"],), daemon=True).start()
                    continue
                msg = u.get("message")
                if not msg:
                    continue
                if msg.get("from", {}).get("id") != ALLOWED:
                    print(f"ignored message from {msg.get('from', {}).get('id')}", flush=True)
                    continue
                handle(msg)
        except Exception as e:
            print(f"poll error: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
