#!/usr/bin/env python3
"""Morning briefing: overnight alerts + cluster health + MRs + zara status -> Telegram (assistant bot).

Runs as `assistant` from a systemd timer at 08:00 Europe/Helsinki, or on demand via /briefing.
Claude only gets read-only tools.
"""
import json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats, tasks_client, tgmd  # noqa: E402

CLAUDE = os.path.expanduser("~/.local/bin/claude")
WORKDIR = os.path.expanduser("~/briefing")
DECISIONS = os.path.expanduser("~/triage/decisions.jsonl")
API = "https://api.telegram.org/bot%s/" % os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_ALLOWED_USER"]
CONTEXTS = [c for c in os.environ.get("BRIEFING_CONTEXTS", os.environ.get("KUBE_CONTEXTS", "")).split(",") if c]
GITLAB_HOST = os.environ.get("GITLAB_HOST", "gitlab.com")
GITLAB_USER = os.environ.get("GITLAB_USER", "")
VERBS = ["get", "top", "describe", "events"]
# kubectl resolves to /opt/zara-assistant/bin/kubectl, a wrapper that only runs read-only commands
MODEL = os.environ.get("BRIEFING_MODEL", "opus")
TOOLS = ["Bash(kubectl:*)", "mcp__grafana-prod"]
RO_ENV = dict(os.environ, PATH="/opt/zara-assistant/bin:" + os.environ.get("PATH", "/usr/bin:/bin"))  # read-only kubectl wrapper first


def merge_requests():
    """Read-only GitLab API calls done here, so Claude needs no GitLab access."""
    out = []
    for label, q in (("To review", "reviewer_username=" + GITLAB_USER), ("Mine", "author_username=" + GITLAB_USER)):
        try:
            p = subprocess.run(["glab", "api", "merge_requests?scope=all&state=opened&per_page=15&" + q],
                               capture_output=True, text=True, timeout=60)
            mrs = json.loads(p.stdout)
            lines = ["  - %s !%s %s (%s, updated %s)%s" % (
                m["references"]["full"].split("!")[0], m["iid"], m["title"][:90], m["author"]["username"],
                m["updated_at"][:10], " [draft]" if m.get("draft") else "") for m in mrs]
            out.append("%s (%d):\n%s" % (label, len(mrs), "\n".join(lines) or "  (none)"))
        except Exception as e:
            out.append("%s: unavailable (%s)" % (label, e))
    return "\n".join(out)


def tg(method, **params):
    data = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v
                                   for k, v in params.items()}).encode()
    return json.load(urllib.request.urlopen(API + method, data=data, timeout=30))


def overnight(hours=10):
    since = time.time() - hours * 3600
    out = []
    try:
        for l in open(DECISIONS):
            d = json.loads(l)
            if d["t"] >= since:
                tag = "WOULD-CALL (quiet hours)" if d.get("quiet") else ("CALLED" if d["call"] else "no call")
                out.append("- %s ~%s: %s → %s (%s)" % (time.strftime("%H:%M", time.localtime(d["t"])),
                                                      d["channel"], d["first_line"][:120], tag, d["reason"]))
    except FileNotFoundError:
        pass
    return "\n".join(out[-60:]) or "(no alerts triaged in this period)"


def zara_status():
    try:
        c = json.loads(stats.collect("24h")[0])["cur"]
        return ("CPU %.0f%%, RAM %.0f%% (%.1f/%.1f GB), disk %.0f%% (%.1f/%.1f GB), temp %.0f°C, uptime %.0f h"
                % (c["c"], c["m"], c["mu"], c["mt"], c["d"], c["du"], c["dt"], c["T"], c["up"]))
    except Exception as e:
        return "unavailable (%s)" % e


def my_tasks():
    try:
        ts = [t for t in tasks_client.api("GET", "/api/tasks", timeout=15) if t["status"] != "done"]
    except Exception as e:
        return "(task list unavailable: %s)" % e
    return "\n".join("#%d P%d %s%s%s" % (t["id"], t["priority"], t["title"], " (doing)" if t["status"] == "doing" else "",
                                         " due " + t["due"] if t.get("due") else "") for t in ts) or "(none)"


PROMPT = """Write my morning briefing as an SRE. Be concise and scannable; it is read on a phone.

Tool rule: only plain single commands are permitted. Run exactly one command per Bash call, with no
loops, pipes, `;`, `&&`, `export` or `VAR=value` prefixes — anything else is rejected. Write kubectl
as `kubectl --context <ctx> get ...` and use its own flags (-A, --field-selector, -o wide) instead of grep.

Use READ-ONLY tools to check, quickly (a handful of commands, not an audit):
1. Kubernetes health per context ({CONTEXTS}): nodes not Ready, pods not
   Running/Completed or with many restarts, recent warning events in prod.
2. Grafana prod (grafana-prod tools): alert rules currently firing.

Already collected for you (do not fetch these yourself):

Open merge requests on {GITLAB}:
%s

Overnight alert triage log:
%s

zara (the board you run on): %s

My open tasks (P1 urgent .. P4 low):
%s

Format (Markdown):
**☀️ Good morning** + one-line overall verdict (all good / needs attention).
### 🚨 Needs attention   (only if something does; most important first, each with a next step)
### 🌙 Overnight          (group repeated alerts; say what would have called me)
### ☸️ Clusters           (one line per context)
### 📋 Today's tasks     (P1s, anything overdue or due today/tomorrow, what I'm doing; then "+N more open")
### 🔀 Merge requests     (to review / mine)
### 🖥 zara               (one line)
Skip sections with nothing to say. No preamble, no closing remarks."""


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    try:
        tg("sendChatAction", chat_id=CHAT, action="typing")
    except Exception:
        pass
    cmd = [CLAUDE, "-p", PROMPT.replace("{CONTEXTS}", ", ".join(CONTEXTS)).replace("{GITLAB}", GITLAB_HOST)
           % (merge_requests(), overnight(), zara_status(), my_tasks()), "--output-format", "json",
           "--max-turns", "40", "--model", MODEL, "--allowedTools", *TOOLS]
    try:
        p = subprocess.run(cmd, cwd=WORKDIR, capture_output=True, text=True, timeout=600, env=RO_ENV)
        text = json.loads(p.stdout)["result"]
    except Exception as e:
        text = "☀️ Morning briefing failed: %r" % e
    try:
        tg("sendRichMessage", chat_id=CHAT, rich_message={"markdown": text})
    except urllib.error.HTTPError:
        for part in tgmd.chunks(text):
            tg("sendMessage", chat_id=CHAT, text=tgmd.to_html(part), parse_mode="HTML",
               disable_web_page_preview="true")


if __name__ == "__main__":
    main()
