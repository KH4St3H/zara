"""Live progress for a Claude Code run, streamed to Telegram via sendRichMessageDraft.

Shows: a <tg-thinking> line with the current step, a list of finished steps, and the
answer as it is being written. Final answers go out via sendRichMessage (native Markdown).
"""
import html
import random
import threading
import time
import urllib.error
from urllib.parse import urlparse

MAX_STEPS_SHOWN = 8
PUSH_EVERY = 1.2      # min seconds between draft updates
KEEPALIVE = 20        # drafts vanish after 30 s without an update


def _clean(s, n=90):
    s = " ".join(str(s).split())
    s = s if len(s) <= n else s[: n - 1] + "…"
    return s.replace("`", "'")


def describe(name, inp):
    """One human line for a tool call."""
    g = inp.get
    if name == "Bash":
        return "🔧 `%s`" % _clean(g("command", ""))
    if name == "Read":
        return "📄 Reading `%s`" % _clean(g("file_path", "").split("/")[-1], 60)
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return "✏️ Editing `%s`" % _clean((g("file_path") or g("notebook_path") or "").split("/")[-1], 60)
    if name in ("Grep", "Glob"):
        return "🔎 Searching `%s`" % _clean(g("pattern", ""), 60)
    if name == "WebSearch":
        return "🌐 Searching the web: %s" % _clean(g("query", ""), 70)
    if name == "WebFetch":
        return "🌐 Fetching %s" % (urlparse(g("url", "")).netloc or "a page")
    if name in ("Task", "Agent"):
        return "🤖 Subagent: %s" % _clean(g("description", ""), 70)
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        icon = "📊" if "grafana" in server else "🔌"
        return "%s %s: %s" % (icon, server, tool.replace("_", " "))
    if name in ("TodoWrite", "ToolSearch", "Skill"):
        return "🗂 %s" % name
    return "⚙️ %s" % name


class LiveView:
    def __init__(self, tg, chat_id):
        self.tg, self.chat_id = tg, chat_id
        self.draft_id = random.randint(1, 2**31 - 1)
        self.steps = []          # finished/started step lines
        self.current = "Thinking…"
        self.text = ""           # answer being written
        self.dirty = True
        self.fail = 0
        self.last = 0.0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    # ---- event intake (called from the reader thread) ----
    def on_event(self, ev):
        t = ev.get("type")
        if t == "stream_event":
            e = ev.get("event") or {}
            et = e.get("type")
            if et == "content_block_start":
                kind = (e.get("content_block") or {}).get("type")
                if kind == "tool_use" and self.text.strip():
                    self._note(self.text)      # text written before a tool call = interim note
                    self.text = ""
                elif kind == "text":
                    self.current = "✍️ Writing…"
                    self.dirty = True
            elif et == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                self.text += e["delta"].get("text", "")
                self.dirty = True
        elif t == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    line = describe(b.get("name", "?"), b.get("input") or {})
                    self.steps.append(line)
                    self.current = line.replace("`", "") + " …"
                    self.dirty = True
        elif t == "user":
            content = (ev.get("message") or {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if b.get("type") == "tool_result" and b.get("is_error") and self.steps:
                        self.steps[-1] += " ✗"
                        self.dirty = True
                self.current = "Thinking…"

    def _note(self, text):
        first = _clean(text.strip().split("\n")[0], 150)
        if first:
            self.steps.append("💭 _%s_" % first.replace("_", " "))
            self.dirty = True

    # ---- draft pushing ----
    def markdown(self):
        md = "<tg-thinking>%s</tg-thinking>" % html.escape(self.current, quote=False)
        shown = self.steps[-MAX_STEPS_SHOWN:]
        if shown:
            more = len(self.steps) - len(shown)
            md += "\n\n" + ("_…%d earlier steps_\n" % more if more else "") + "\n".join("- " + s for s in shown)
        if self.text.strip():
            md += "\n\n" + self.text[-3000:]
        return md

    def push(self):
        if self.fail >= 3:
            return
        try:
            self.tg("sendRichMessageDraft", chat_id=self.chat_id, draft_id=self.draft_id, can_stop=True,
                    rich_message={"markdown": self.markdown()})
            self.fail = 0
        except urllib.error.HTTPError as e:
            if e.code != 429:  # rate limits are fine to skip; other errors count
                self.fail += 1
                print("draft failed:", e.read().decode()[:200], flush=True)
        except Exception as e:
            self.fail += 1
            print("draft failed:", e, flush=True)
        self.dirty = False
        self.last = time.time()

    def _loop(self):
        self.push()
        while not self._stop.wait(0.4):
            now = time.time()
            if (self.dirty and now - self.last >= PUSH_EVERY) or now - self.last >= KEEPALIVE:
                self.push()

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._stop.set()

    def summary(self):
        """Collapsed list of steps to append under the final answer."""
        tools = [s for s in self.steps if not s.startswith("💭")]
        if not tools:
            return ""
        return ("\n\n<details><summary>🔧 %d step%s</summary>\n\n%s\n\n</details>"
                % (len(tools), "" if len(tools) == 1 else "s", "\n".join("- " + s for s in tools)))
