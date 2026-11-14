#!/usr/bin/env python3
"""Mattermost -> Telegram forwarder with reply-back.

Forwards DMs, group DMs and @mentions of the token owner to a Telegram group.
Replying (Telegram "reply") to a forwarded message from TELEGRAM_ALLOWED_USER
posts the reply back to the same Mattermost channel/thread.
"""
import json, os, re, sqlite3, threading, time, urllib.error, urllib.parse, urllib.request
import websocket  # Debian: python3-websocket
import sensitive, tgmd

MM = os.environ["MM_URL"].rstrip("/")
MM_TOKEN = os.environ["MM_TOKEN"]
TEAM_ID = os.environ.get("MM_TEAM_ID", "")
TG = "https://api.telegram.org/bot%s/" % os.environ["MM_TG_BOT_TOKEN"]
CHAT = os.environ.get("MM_TG_CHAT_ID", "").strip()
OWNER = int(os.environ["TELEGRAM_ALLOWED_USER"])
ALERT_CHAT = os.environ.get("MM_TG_ALERT_CHAT_ID", "").strip()
ALERT_CHANNELS = {c.strip() for c in os.environ.get("MM_ALERT_CHANNELS", "").split(",") if c.strip()}
TRIAGE_URL = os.environ.get("TRIAGE_URL", "http://127.0.0.1:8098/alert")
REDACT = os.environ.get("SENSITIVE_FILTER", "1") != "0"   # hide secret-looking text before it leaves Mattermost
DB_PATH = os.path.join(os.environ.get("STATE_DIRECTORY", "."), "map.db")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS map (tg_id INTEGER PRIMARY KEY, channel_id TEXT, root_id TEXT)")
db_lock = threading.Lock()


def log(*a):
    print(*a, flush=True)


# ---------- HTTP helpers ----------
def mm(method, path, body=None):
    req = urllib.request.Request(
        MM + "/api/v4" + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + MM_TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def tg(method, **params):
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                   for k, v in params.items()}).encode()
    with urllib.request.urlopen(TG + method, data=data, timeout=70) as r:
        return json.load(r)


# ---------- Mattermost lookups (cached) ----------
_users, _channels = {}, {}


def username(uid):
    if uid not in _users:
        try:
            _users[uid] = mm("GET", "/users/" + uid)["username"]
        except Exception:
            _users[uid] = uid[:8]
    return _users[uid]


def channel(cid):
    if cid not in _channels:
        _channels[cid] = mm("GET", "/channels/" + cid)
    return _channels[cid]


_me = mm("GET", "/users/me")
ME, MY_USERNAME = _me["id"], _me["username"]
TEAM_NAME = ""
if TEAM_ID:
    try:
        TEAM_NAME = mm("GET", "/teams/" + TEAM_ID)["name"]
    except Exception:
        pass


# ---------- Mattermost -> Telegram ----------
def render(post):
    """Message text plus Slack-style attachments (used by Grafana/Gatus/ArgoCD webhooks)."""
    parts = [post.get("message", "")]
    for a in (post.get("props") or {}).get("attachments") or []:
        for k in ("pretext", "title", "text"):
            if a.get(k):
                parts.append(a[k])
        for f in a.get("fields") or []:
            parts.append("%s: %s" % (f.get("title", ""), f.get("value", "")))
        if a.get("title_link"):
            parts.append(a["title_link"])
    return "\n".join(p for p in parts if p).strip()


def send_chunks(chat, text):
    ids = []
    for part in tgmd.chunks(text):
        try:
            r = tg("sendMessage", chat_id=chat, text=tgmd.to_html(part), parse_mode="HTML",
                   disable_web_page_preview="true")
        except urllib.error.HTTPError:  # formatting rejected -> plain text
            r = tg("sendMessage", chat_id=chat, text=part, disable_web_page_preview="true")
        ids.append(r["result"]["message_id"])
    return ids


FIRING_MARK = ("🔴", "🔥", "❌", "🚨", "⛔")
RESOLVED_MARK = ("✅", "🟢", "☑")
GREEN = {"good", "#36a64f", "#2eb886", "#00ff00", "green"}


def alert_state(post, body):
    """-> (state, firing_lines, resolved_count); state is "resolved", "firing" or "mixed".
    Item lines (message + attachment text, NOT titles) decide: Grafana/Argo titles like "🔴 2 Argo CD"
    stay red even when the items listed below are mostly resolved."""
    atts = (post.get("props") or {}).get("attachments") or []
    if atts and all(str(a.get("color", "")).lower() in GREEN for a in atts):
        return "resolved", [], 0
    items = (post.get("message") or "").splitlines()
    for a in atts:
        items += (a.get("text") or "").splitlines()
    items = [l.strip() for l in items if l.strip()]
    firing = [l for l in items if l.startswith(FIRING_MARK)]
    resolved = sum(1 for l in items if l.startswith(RESOLVED_MARK))
    up = body.upper()
    if "[RESOLVED" in up or (resolved and not firing):
        return "resolved", [], resolved
    if body.lstrip().startswith(RESOLVED_MARK) and not firing:
        return "resolved", [], resolved
    if firing and resolved:
        return "mixed", firing, resolved
    return "firing", firing, 0


def clean(text):
    """-> (text with secrets masked, note line or "")."""
    if not REDACT:
        return text, ""
    try:
        text, n = sensitive.redact(text)
    except Exception as e:          # never lose a message because of the filter
        log("redact failed:", repr(e))
        return text, ""
    return text, ("🔒 %d sensitive-looking value(s) hidden — open in Mattermost to see them" % n) if n else ""


def post_json(url, payload):
    try:
        urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"}), timeout=5).read()
    except Exception as e:
        log("request to", url, "failed:", repr(e))


def forward_alert(post):
    ch = channel(post["channel_id"])
    body = render(post)
    state, firing, n_resolved = alert_state(post, body)
    body, note = clean(body)
    firing = [clean(l)[0] for l in firing]
    up = body.upper()
    if state == "resolved":
        icon = "🟢"
    elif state == "mixed":
        icon = "🟠"
    else:
        icon = "🔴" if ("FIRING" in up or "CRITICAL" in up or "DOWN" in up or firing) else "⚠️"
    text = "%s ~%s\n%s" % (icon, ch.get("display_name") or ch.get("name"), body or "(empty alert)")
    if note:
        text += "\n" + note
    if TEAM_NAME:
        text += "\n%s/%s/pl/%s" % (MM, TEAM_NAME, post["id"])
    ids = send_chunks(ALERT_CHAT, text)
    props = post.get("props") or {}
    automated = str(props.get("from_webhook")) == "true" or str(props.get("from_bot")) == "true"
    if not automated:
        return
    name = ch.get("display_name") or ch.get("name")
    if state == "resolved":       # lets triage drop/cancel a pending call for this channel
        post_json(TRIAGE_URL.rsplit("/", 1)[0] + "/resolved", {"channel_id": post["channel_id"], "channel": name})
        return
    if state == "mixed":          # only the still-firing items go to triage
        title = body.splitlines()[0] if body else ""
        tbody = ("%s\n(%d item(s) in this notification are already RESOLVED and omitted; still firing:)\n%s"
                 % (title, n_resolved, "\n".join(firing)))
    else:
        tbody = body
    post_json(TRIAGE_URL, {"channel_id": post["channel_id"], "channel": name, "text": tbody,
                           "tg_msg_id": ids[0] if ids else None})


# Owner replies "/investigate [focus]" (or "!investigate") in a thread -> Claude investigates the
# thread's root post (the alert) and answers in that thread.
INVESTIGATE_CMD = re.compile(r"^\s*(?:[/!]investigate\b(.*)|investigate\s*)$", re.I | re.S)
CMD_MAX_AGE = 30 * 60 * 1000      # don't act on stale commands replayed by catch-up


def request_investigation(post, focus):
    try:
        root = mm("GET", "/posts/" + post["root_id"])
        ch = channel(post["channel_id"])
    except Exception as e:
        return log("investigate: cannot load thread root:", repr(e))
    log("investigate requested in ~%s" % (ch.get("display_name") or ch.get("name")))
    post_json(TRIAGE_URL.rsplit("/", 1)[0] + "/investigate",
              {"channel_id": post["channel_id"], "root_id": post["root_id"],
               "channel": ch.get("display_name") or ch.get("name"), "text": clean(render(root))[0] or "(empty post)",
               "focus": focus[:500]})


def forward(post, channel_type, sender):
    ch = channel(post["channel_id"])
    if channel_type == "D":
        head = "💬 DM from @%s" % sender
    elif channel_type == "G":
        head = "👥 @%s in group DM (%s)" % (sender, ch.get("display_name", ""))
    else:
        head = "📣 @%s mentioned you in ~%s" % (sender, ch.get("display_name") or ch.get("name"))
    if post.get("root_id"):
        head += " (thread)"
    body, note = clean(render(post))
    if note:
        body += "\n" + note
    if post.get("file_ids"):
        body += "\n📎 %d attachment(s)" % len(post["file_ids"])
    if TEAM_NAME:
        body += "\n%s/%s/pl/%s" % (MM, TEAM_NAME, post["id"])
    text = head + ":\n" + body
    root = post.get("root_id") or post["id"]
    for mid in send_chunks(CHAT, text):
        with db_lock:
            db.execute("INSERT OR REPLACE INTO map VALUES (?,?,?)", (mid, post["channel_id"], root))
            db.commit()


def on_event(ev):
    if ev.get("event") != "posted":
        return
    d = ev["data"]
    post = json.loads(d["post"])
    if post["id"] in _done:          # already handled (live event vs. catch-up overlap)
        return
    _done[post["id"]] = True
    while len(_done) > 2000:
        _done.pop(next(iter(_done)))
    if post.get("create_at", 0) > last_seen["t"]:
        last_seen["t"] = post["create_at"]
    # Skip what the owner typed, but NOT webhook posts: incoming webhooks created by the owner's
    # account post as the owner (most alert channels work like that).
    own = post.get("user_id") == ME and str((post.get("props") or {}).get("from_webhook")) != "true"
    if own and post.get("root_id"):
        m = INVESTIGATE_CMD.match(post.get("message") or "")
        if m and time.time() * 1000 - post.get("create_at", 0) < CMD_MAX_AGE:
            request_investigation(post, (m.group(1) or "").strip())
        return
    if own or post.get("type", "").startswith("system_"):
        return
    if post["channel_id"] in ALERT_CHANNELS:
        if ALERT_CHAT:
            forward_alert(post)
        return  # alert channels never go to the DM/mention group
    ctype = d.get("channel_type")
    mentions = json.loads(d.get("mentions") or "[]")
    if ctype in ("D", "G") or ME in mentions:
        sender = (d.get("sender_name") or "").lstrip("@") or username(post["user_id"])
        forward(post, ctype, sender)


CATCHUP_MAX = 12 * 3600 * 1000    # never replay more than 12 h
_done: dict = {}                  # recently handled post ids (ordered, bounded)
last_seen = {"t": int(time.time() * 1000)}


def catch_up():
    """After a reconnect: replay posts created while the websocket was down."""
    since = max(last_seen["t"], int(time.time() * 1000) - CATCHUP_MAX)
    n = 0
    for team in mm("GET", "/users/me/teams"):
        for c in mm("GET", "/users/me/teams/%s/channels" % team["id"]):
            if c.get("last_post_at", 0) <= since:
                continue
            posts = mm("GET", "/channels/%s/posts?since=%d" % (c["id"], since)).get("posts", {})
            for p in sorted(posts.values(), key=lambda p: p["create_at"]):
                if p["create_at"] <= since or p["id"] in _done or p.get("delete_at"):
                    continue
                mentioned = ("@" + MY_USERNAME) in (p.get("message") or "")
                on_event({"event": "posted", "data": {"post": json.dumps(p), "channel_type": c["type"],
                                                       "mentions": json.dumps([ME] if mentioned else [])}})
                n += 1
    if n:
        log("mattermost: caught up %d missed post(s)" % n)


def mm_loop():
    url = MM.replace("https://", "wss://").replace("http://", "ws://") + "/api/v4/websocket"
    backoff = 2
    while True:
        ws = None
        try:
            ws = websocket.create_connection(url, header=["Authorization: Bearer " + MM_TOKEN], timeout=30)
            log("mattermost: connected")
            backoff = 2
            try:
                catch_up()
            except Exception as e:
                log("catch-up failed:", repr(e))
            ws.settimeout(30)
            seq, last_rx = 0, time.time()
            while True:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    # half-open TCP connections never error out: require an answer to an app-level ping
                    if time.time() - last_rx > 90:
                        raise ConnectionError("no reply from Mattermost for 90 s")
                    seq += 1
                    ws.send(json.dumps({"seq": seq, "action": "ping"}))
                    continue
                if not raw:
                    raise ConnectionError("socket closed")
                last_rx = time.time()
                try:
                    on_event(json.loads(raw))
                except Exception as e:
                    log("event error:", repr(e))
        except Exception as e:
            log("mattermost: disconnected:", repr(e), "- retrying in", backoff, "s")
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


# ---------- Telegram -> Mattermost (replies) ----------
def tg_loop():
    offset = None
    while True:
        try:
            params = {"timeout": 60,
                      "allowed_updates": ["message", "channel_post", "my_chat_member", "callback_query"]}
            if offset:
                params["offset"] = offset
            for u in tg("getUpdates", **params).get("result", []):
                offset = u["update_id"] + 1
                cq = u.get("callback_query")
                if cq:  # buttons under triage decisions -> alert-triage service
                    mine = cq.get("from", {}).get("id") == OWNER
                    label = {"inv": "🔍 Investigating…", "fb+": "👍 Thanks!", "fb-": "👎 Noted"}
                    try:
                        tg("answerCallbackQuery", callback_query_id=cq["id"],
                           text=label.get(cq.get("data"), "OK") if mine else "Not allowed")
                    except Exception:
                        pass
                    if mine and cq.get("message"):
                        try:
                            urllib.request.urlopen(urllib.request.Request(
                                TRIAGE_URL.rsplit("/", 1)[0] + "/callback", method="POST",
                                headers={"Content-Type": "application/json"},
                                data=json.dumps({"data": cq.get("data"), "from_id": OWNER,
                                                 "message_id": cq["message"]["message_id"]}).encode()),
                                timeout=5).read()
                        except Exception as e:
                            log("callback forward failed:", repr(e))
                    continue
                for k in ("my_chat_member", "channel_post"):
                    if k in u:
                        c = u[k]["chat"]
                        if str(c["id"]) not in (CHAT, ALERT_CHAT):
                            log("discovery: %s in chat %s %s %r" % (k, c["id"], c.get("type"), c.get("title")))
                m = u.get("message") or {}
                chat_id = str(m.get("chat", {}).get("id"))
                if not CHAT or chat_id not in (CHAT,):
                    if m and chat_id != ALERT_CHAT:
                        log("discovery: message in chat", chat_id, repr(m.get("chat", {}).get("title")))
                    continue
                if chat_id != CHAT or m.get("from", {}).get("id") != OWNER:
                    continue
                rep = m.get("reply_to_message")
                text = m.get("text") or m.get("caption")
                if not rep or not text:
                    continue
                with db_lock:
                    row = db.execute("SELECT channel_id, root_id FROM map WHERE tg_id=?",
                                     (rep["message_id"],)).fetchone()
                if not row:
                    continue
                try:
                    mm("POST", "/posts", {"channel_id": row[0], "root_id": row[1], "message": text})
                    tg("setMessageReaction", chat_id=CHAT, message_id=m["message_id"],
                       reaction=[{"type": "emoji", "emoji": "👍"}])
                except Exception as e:
                    tg("sendMessage", chat_id=CHAT, reply_to_message_id=m["message_id"],
                       text="❌ Not sent to Mattermost: %r" % e)
        except Exception as e:
            log("telegram poll error:", repr(e))
            time.sleep(5)


if __name__ == "__main__":
    log("mm-forwarder starting; me=%s team=%s chat=%s alert_chat=%s alert_channels=%d"
        % (ME, TEAM_NAME, CHAT or "(discovery mode)", ALERT_CHAT or "(none)", len(ALERT_CHANNELS)))
    if CHAT:
        threading.Thread(target=mm_loop, daemon=True).start()
    tg_loop()
