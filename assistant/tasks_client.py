"""Talk to the zara-tasks service and render tasks for Telegram (HTML parse mode)."""
import html, json, os, urllib.error, urllib.request

URL = os.environ.get("TASKS_API", "http://127.0.0.1:8082")
PRIO = {1: "🔴", 2: "🟠", 3: "🔵", 4: "⚪"}


class TasksError(Exception):
    pass


def api(method, path, body=None, timeout=200):
    req = urllib.request.Request(URL + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + os.environ.get("TASKS_TOKEN", ""),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = None
        raise TasksError(msg or str(e))
    except OSError as e:
        raise TasksError("task service unreachable (%s)" % e)


def line(t, notes=False):
    s = "%s <b>#%d</b> %s" % (PRIO.get(t["priority"], ""), t["id"], html.escape(t["title"]))
    if t["status"] == "doing":
        s += " <i>(doing)</i>"
    if t.get("due"):
        s += " · 📅 %s" % t["due"]
    if notes and t.get("notes"):
        s += "\n<blockquote>%s</blockquote>" % html.escape(t["notes"][:900])
    return s


def created_text(t):
    s = "📌 Added task, <b>P%d</b>\n%s" % (t["priority"], line(t, notes=True))
    return s


def list_view(login_url=None, limit=25):
    """-> (html text, inline keyboard) for the /tasks message."""
    ts = [t for t in api("GET", "/api/tasks", timeout=15) if t["status"] != "done"]
    if not ts:
        text = "📋 <b>Tasks</b>\nNothing open. 🎉"
    else:
        urgent = sum(1 for t in ts if t["priority"] == 1)
        text = "📋 <b>Tasks</b> · %d open%s\n\n" % (len(ts), " · %d urgent" % urgent if urgent else "")
        text += "\n".join(line(t) for t in ts[:limit])
        if len(ts) > limit:
            text += "\n… and %d more" % (len(ts) - limit)
    buttons = [{"text": "✅ #%d" % t["id"], "callback_data": "td:%d" % t["id"]} for t in ts[:12]]
    kb = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    last = [{"text": "🔄", "callback_data": "tr"}]
    if login_url:
        last.insert(0, {"text": "🌐 Open task list", "url": login_url})
    kb.append(last)
    return text, {"inline_keyboard": kb}
