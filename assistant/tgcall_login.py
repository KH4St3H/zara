#!/usr/bin/env python3
"""One-time interactive login for the zara caller account. Run as root on zara."""
import getpass, os, re, sys
from telethon.sync import TelegramClient

ENV = "/etc/zara-assistant/env"
STATE = "/var/lib/tgcaller"
env = dict(l.split("=", 1) for l in open(ENV).read().splitlines() if "=" in l)

if "TG_API_ID" not in env or "TG_API_HASH" not in env:
    api_id = input("api_id (from my.telegram.org): ").strip()
    api_hash = getpass.getpass("api_hash (hidden): ").strip()
    if not api_id.isdigit() or not re.fullmatch(r"[0-9a-f]{32}", api_hash):
        sys.exit("That doesn't look like a valid api_id / api_hash.")
    with open(ENV, "a") as f:
        f.write("TG_API_ID=%s\nTG_API_HASH=%s\n" % (api_id, api_hash))
    env["TG_API_ID"], env["TG_API_HASH"] = api_id, api_hash

os.makedirs(STATE, mode=0o700, exist_ok=True)
with TelegramClient(os.path.join(STATE, "zara"), int(env["TG_API_ID"]), env["TG_API_HASH"]) as c:
    # .start() prompts for the phone number, login code and 2FA password if needed
    me = c.get_me()
    print("Logged in as %s (id %s)." % (me.first_name, me.id))
    print("Now send any message from YOUR main Telegram account to this account,")
    print("so it is allowed to call you.")
