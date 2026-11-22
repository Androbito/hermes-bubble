"""HermesKey voice worker — a long-running process behind the 🌐 bubble.

Keeps the Whisper model and Hermes imports warm, and talks to the running Hermes gateway's
local API server (API_SERVER_ENABLED, default 127.0.0.1:8642) instead of spawning
`hermes chat -q` per question.
Falls back to the CLI path in ask.py automatically if the API server is unreachable.

Control: Unix socket $HERMES_HOME/bubble/worker.sock, one command per connection:
  toggle  start a conversation turn, or stop the one in progress
  stop    stop the current turn (bubble stays open)
  close   end the conversation (✕): stop + start a fresh Hermes session next time
  ping    health check → "pong"

Lifecycle (HermesKey respawns the worker whenever it exits):
  - restarts itself when idle after 6 h uptime or > 1.5 GB RSS
  - a voice conversation = one Hermes API session; a new one starts on ✕ close,
    after 10 min of silence, or after 30 turns (Hermes also auto-compresses long contexts)
"""
import importlib.util
import json
import os
import re
import resource
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent
sys.path.insert(0, str(RUNTIME))
from config import CFG, DATA, HERMES_HOME  # noqa: E402

BASE = DATA
SOCK = BASE / "worker.sock"
API_SESSION = BASE / "api_session.json"
API_URL = CFG["api_url"]
MAX_UPTIME = 6 * 3600
MAX_RSS_MB = 1536
MAX_TURNS = int(CFG["max_turns"])
IDLE_RESET = int(CFG["idle_reset_minutes"]) * 60

spec = importlib.util.spec_from_file_location("ask", str(RUNTIME / "ask.py"))
ask = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ask)


def log(m):
    ask.log("[worker] " + m)


STARTED = time.time()
_lock = threading.Lock()
_active = {"thread": None, "cancel": None}


# ── Hermes API session management ────────────────────────────────────────────

def _api_key():
    env = HERMES_HOME / ".env"
    if not env.exists():
        return ""
    for line in env.read_text().splitlines():
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _load_session():
    try:
        return json.loads(API_SESSION.read_text())
    except Exception:
        return {}


def _save_session(s):
    tmp = API_SESSION.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    os.replace(tmp, API_SESSION)


def current_session():
    s = _load_session()
    fresh = (not s.get("id") or time.time() - s.get("ts", 0) > IDLE_RESET
             or s.get("turns", 0) >= MAX_TURNS)
    if fresh:
        s = {"id": f"voice-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
             "ts": time.time(), "turns": 0}
        _save_session(s)
        log(f"new voice session {s['id']}")
    return s


def reset_session():
    try:
        API_SESSION.unlink()
    except OSError:
        pass


def ask_via_api(text):
    """One turn through the gateway API server; returns the answer, or None if unreachable."""
    key = _api_key()
    if not key:
        return None
    s = current_session()
    payload = {"model": ask.VOICE_MODEL or "hermes-agent", "stream": False,
               "messages": [{"role": "user", "content": ask.VOICE_PREFIX + ask.lang_hint() + text}]}
    if ask.VOICE_PROVIDER:
        payload["provider"] = ask.VOICE_PROVIDER
    body = json.dumps(payload).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "X-Hermes-Session-Id": s["id"]})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        log(f"api HTTP {e.code}: {detail}")
        if e.code in (404, 409):          # session refused/retired → start fresh next time
            reset_session()
        return None if e.code >= 500 or e.code in (401, 403) else "Sorry, something went wrong talking to Hermes."
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log(f"api unreachable ({e}); using CLI fallback")
        return None
    msg = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    s["ts"], s["turns"] = time.time(), s.get("turns", 0) + 1
    _save_session(s)
    # Notifications open this session in Terminal.
    ask.SESSION.write_text(json.dumps({"id": s["id"], "ts": time.time(), "via": "api"}))
    return msg.strip() or "I didn't get an answer, sorry."


_cli_ask = ask.ask_hermes


def ask_hermes(text):
    t = time.monotonic()
    ans = ask_via_api(text)
    route = "api"
    if ans is None:
        ans, route = _cli_ask(text), "cli"
    log(f"timing: hermes via {route} {time.monotonic() - t:.1f}s")
    return ans


ask.ask_hermes = ask_hermes   # run() in ask.py looks this name up at call time


# ── conversation turns ───────────────────────────────────────────────────────

from tools import voice_mode as vm                       # noqa: E402  (warm imports)
from tools.tts_tool import text_to_speech_tool           # noqa: E402

ask._preload_stt()                                       # Whisper stays loaded for the worker's life


def _conversation(cancel):
    ask.PIDF.write_text(str(os.getpid()))
    try:
        answer = ask.run(vm, text_to_speech_tool, cancel)
        while answer and ask.conv_open() and not cancel.is_set():
            answer = ask.run(vm, text_to_speech_tool, cancel, followup=True)
    except Exception:
        import traceback
        log("turn crashed: " + traceback.format_exc()[-1500:])
    finally:
        ask.set_state("ready" if ask.conv_open() else "idle")
        with _lock:
            _active["thread"] = None
        maybe_recycle()


def _stop():
    with _lock:
        c = _active["cancel"]
    if c:
        c.set()
    vm.stop_playback()


def toggle():
    with _lock:
        t = _active["thread"]
        if t and t.is_alive():
            busy = True
        else:
            busy = False
            cancel = threading.Event()
            _active["cancel"] = cancel
            t = threading.Thread(target=_conversation, args=(cancel,), daemon=True)
            _active["thread"] = t
    if busy:
        log("stop requested")
        _stop()
    else:
        t.start()


def maybe_recycle():
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)  # bytes on macOS
    if time.time() - STARTED > MAX_UPTIME or rss_mb > MAX_RSS_MB:
        log(f"recycling (uptime {int(time.time() - STARTED)}s, rss {rss_mb:.0f} MB)")
        threading.Timer(0.5, lambda: os._exit(0)).start()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        cmd = self.rfile.readline().decode().strip()
        if cmd == "ping":
            self.wfile.write(b"pong\n")
        elif cmd == "toggle":
            toggle()
            self.wfile.write(b"ok\n")
        elif cmd == "stop":
            _stop()
            self.wfile.write(b"ok\n")
        elif cmd == "close":
            _stop()
            reset_session()
            log("conversation closed")
            self.wfile.write(b"ok\n")
        elif cmd.startswith("dotext "):           # test hook: full routing (actions too), no audio
            text = cmd[len("dotext "):]
            lang = "fr" if re.search(r"\b(le|la|les|un|une|mets|ouvre|cherche|appelle|sur|de)\b", text, re.I) else "en"
            lang = lang if lang in ask.VOICE_LANGS else ask.VOICE_LANGS[0]
            ask.LAST_LANG = lang
            ctx = ask.action_ctx(vm, text_to_speech_tool)
            yt = ask.parse_youtube_command(text)
            if yt:
                ans = "YT: " + ask.youtube_play(*yt)
            else:
                ans = ask.actions.fast_route(text, lang, ctx)
                if ans:
                    ans = "FAST: " + ans
                else:
                    raw = ask_hermes(text)
                    acts, rest = ask.actions.extract_actions(raw)
                    ans = ("LLM-ACTION " + json.dumps(acts) + " → " +
                           " ".join(filter(None, (ask.actions.run_action(a, lang, ctx) for a in acts)))
                           ) if acts else "LLM: " + raw
            self.wfile.write((ans.replace("\n", " ") + "\n").encode())
        elif cmd.startswith("asktext "):          # test hook: text in → answer out, no audio
            ans = ask_hermes(cmd[len("asktext "):])
            self.wfile.write((ans.replace("\n", " ") + "\n").encode())
        else:
            self.wfile.write(b"?\n")


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    # Only one worker: if a live one answers, exit.
    if SOCK.exists():
        try:
            c = socket.socket(socket.AF_UNIX)
            c.settimeout(1)
            c.connect(str(SOCK))
            c.sendall(b"ping\n")
            if c.recv(16).startswith(b"pong"):
                log("another worker is running; exiting")
                return
        except OSError:
            pass
        SOCK.unlink()
    srv = Server(str(SOCK), Handler)
    os.chmod(SOCK, 0o600)
    log(f"ready (pid {os.getpid()}, stt={ask.VOICE_STT_MODEL})")
    try:
        srv.serve_forever()
    finally:
        try:
            SOCK.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
