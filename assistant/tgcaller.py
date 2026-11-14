#!/usr/bin/env python3
"""Telegram voice-call alerter.

A second Telegram *user* account (logged in once via tgcall_login.py) calls
TELEGRAM_ALLOWED_USER and reads a message aloud (espeak-ng).

Requests: POST http://127.0.0.1:8099/call  body: plain text to speak.
Behaviour: up to CALL_ATTEMPTS tries, CALL_RETRY_SECONDS apart, if unanswered.
Declining the call counts as acknowledged. Requests arriving while a call is
pending are merged into the next call instead of queuing many calls.
"""
import asyncio, logging, os, re, subprocess, tempfile, time, wave
from telethon import TelegramClient
from pytgcalls import PyTgCalls
from pytgcalls.exceptions import CallBusy, CallDeclined, CallDiscarded, TimedOutAnswer
from pytgcalls.types import CallConfig, MediaStream

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("tgcaller")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
OWNER = int(os.environ["TELEGRAM_ALLOWED_USER"])
STATE = os.environ.get("STATE_DIRECTORY", "/var/lib/tgcaller")
ATTEMPTS = int(os.environ.get("CALL_ATTEMPTS", "3"))
RETRY = int(os.environ.get("CALL_RETRY_SECONDS", "60"))
RING = int(os.environ.get("CALL_RING_SECONDS", "45"))

client = TelegramClient(os.path.join(STATE, "zara"), API_ID, API_HASH)
calls = PyTgCalls(client)
pending: list[str] = []
wake = asyncio.Event()
acked_at = 0.0   # set by POST /ack (zara's button); stops remaining retries


VOICE_PATH = os.environ.get("PIPER_VOICE", "/opt/zara-assistant/voices/en_US-lessac-medium.onnx")
voice = None  # loaded once in main(); espeak-ng is the fallback


def speakable(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)          # don't read links aloud
    text = re.sub(r"[_~#*`|<>\[\]{}]+", " ", text)
    text = re.sub(r"(?<=\w)-(?=\w)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tts(text: str) -> tuple[str, float]:
    f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=STATE)
    f.close()
    t = speakable(text)[:700]
    spoken = "Alert from zara. " + t + ". I repeat. " + t
    if voice is not None:
        with wave.open(f.name, "wb") as w:
            voice.synthesize_wav(spoken, w)
    else:
        subprocess.run(["espeak-ng", "-s", "140", "-w", f.name, spoken], check=True)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", f.name], capture_output=True, text=True).stdout or 20)
    return f.name, dur


async def call_once(text: str) -> str:
    wav, dur = await asyncio.to_thread(tts, text)
    try:
        await calls.play(OWNER, MediaStream(wav, video_flags=MediaStream.Flags.IGNORE),
                         config=CallConfig(timeout=RING))
        log.info("answered; speaking %.0fs", dur)
        await asyncio.sleep(dur + 3)
        try:
            await calls.leave_call(OWNER)
        except Exception:
            pass
        return "answered"
    except CallDeclined:
        return "declined"
    except (TimedOutAnswer, CallDiscarded):
        return "no-answer"
    except CallBusy:
        return "busy"
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass


async def worker():
    while True:
        await wake.wait()
        wake.clear()
        if not pending:
            continue
        text = " Next alert. ".join(pending[:5]) + (" And more alerts." if len(pending) > 5 else "")
        pending.clear()
        started = time.time()
        for attempt in range(1, ATTEMPTS + 1):
            if acked_at > started:
                log.info("acknowledged — skipping remaining attempts")
                break
            try:
                result = await call_once(text)
            except Exception as e:
                result = "error: %r" % e
            log.info("call attempt %d/%d: %s", attempt, ATTEMPTS, result)
            if result in ("answered", "declined"):
                break
            if attempt < ATTEMPTS:
                await asyncio.sleep(RETRY)
                if pending:  # fold in anything new that arrived meanwhile
                    text += " Next alert. " + " Next alert. ".join(pending)
                    pending.clear()


async def handle_http(reader, writer):
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        lines = head.decode(errors="replace").split("\r\n")
        method, path = lines[0].split(" ")[:2]
        length = 0
        for l in lines[1:]:
            if l.lower().startswith("content-length:"):
                length = int(l.split(":", 1)[1])
        body = (await reader.readexactly(length)).decode(errors="replace").strip() if length else ""
        if method == "POST" and path == "/call" and body:
            pending.append(body[:600])
            wake.set()
            status, msg = "202 Accepted", "queued\n"
        elif method == "POST" and path == "/ack":
            global acked_at
            acked_at = time.time()
            pending.clear()
            status, msg = "200 OK", "acknowledged\n"
        else:
            status, msg = "400 Bad Request", "POST /call with text body\n"
    except Exception as e:
        status, msg = "500 Internal Server Error", "%r\n" % e
    writer.write(("HTTP/1.1 %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                  % (status, len(msg), msg)).encode())
    await writer.drain()
    writer.close()


async def main():
    global voice
    try:
        from piper import PiperVoice
        voice = await asyncio.to_thread(PiperVoice.load, VOICE_PATH)
        log.info("piper voice loaded: %s", os.path.basename(VOICE_PATH))
    except Exception as e:
        log.warning("piper unavailable (%r); falling back to espeak-ng", e)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Not logged in — run tgcall_login.py first.")
    me = await client.get_me()
    await client.get_dialogs()  # populate entity cache so OWNER can be resolved
    await calls.start()
    log.info("logged in as %s (%s); calling user %s", me.first_name, me.id, OWNER)
    server = await asyncio.start_server(handle_http, "127.0.0.1", 8099)
    asyncio.create_task(worker())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
