#!/usr/bin/env python3
"""Teach the sensitivity filter which words are normal in your Mattermost (jargon, names, Finglish).

Reads the last DAYS of posts from every channel you are in and writes $STATE_DIRECTORY/bigram_local.json
(words seen in >= 3 different messages + their letter-pair counts; no message text is stored).
Run as the mm-forwarder service (it owns the state dir):
  systemd-run --pipe --wait -p DynamicUser=yes -p StateDirectory=mm-forwarder \
      -p EnvironmentFile=/etc/zara-assistant/env python3 /opt/zara-assistant/sensitive_train.py [DAYS] [--report]
--report also prints how many recent messages would be redacted, per rule, with tokens masked.
"""
import json, os, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sensitive  # noqa: E402

MM = os.environ["MM_URL"].rstrip("/") + "/api/v4"
HDR = {"Authorization": "Bearer " + os.environ["MM_TOKEN"]}


def get(path):
    with urllib.request.urlopen(urllib.request.Request(MM + path, headers=HDR), timeout=60) as r:
        return json.load(r)


def texts(days):
    since = int((time.time() - days * 86400) * 1000)
    out = []
    for team in get("/users/me/teams"):
        for c in get("/users/me/teams/%s/channels" % team["id"]):
            if c.get("last_post_at", 0) < since:
                continue
            for page in range(50):                      # newest first, 200 per page
                r = get("/channels/%s/posts?page=%d&per_page=200" % (c["id"], page))
                ps = [r["posts"][i] for i in r.get("order", [])]
                for p in ps:
                    if p.get("create_at", 0) >= since and not p.get("type", "").startswith("system_"):
                        t = p.get("message") or ""
                        for a in (p.get("props") or {}).get("attachments") or []:
                            t += "\n" + "\n".join(a.get(k) or "" for k in ("pretext", "title", "text"))
                        out.append((p["create_at"], t))
                if len(ps) < 200 or not ps or ps[-1].get("create_at", 0) < since:
                    break
    return out


def main():
    days = int(next((a for a in sys.argv[1:] if a.isdigit()), "60"))
    posts = texts(days)
    model = sensitive.build_local_model([t for _, t in posts])
    path = sensitive.LOCAL_MODEL
    with open(path + ".tmp", "w") as f:
        json.dump(model, f)
    os.replace(path + ".tmp", path)
    print("trained on %d posts from %d days: %d known words -> %s" % (len(posts), days, len(model["words"]), path))
    if "--report" in sys.argv:
        import importlib
        importlib.reload(sensitive)
        week = int((time.time() - 7 * 86400) * 1000)
        recent = [t for ts, t in posts if ts >= week]
        hits, why = 0, {}
        for t in recent:
            for tok in t.split():
                r = sensitive.why_sensitive(tok)
                if r:
                    tok = tok.strip(sensitive.STRIP)
                    why.setdefault(r, []).append("%s…(%d)" % (tok[:3], len(tok)))
        hits = sum(1 for t in recent if sensitive.redact(t)[1])
        print("last 7 days: %d of %d posts would have something redacted" % (hits, len(recent)))
        for r, toks in why.items():
            print("  %-14s %4d  e.g. %s" % (r, len(toks), ", ".join(toks[:12])))


if __name__ == "__main__":
    main()
