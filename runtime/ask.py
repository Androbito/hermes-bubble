"""Hold-🌐 voice ask: beep → record until silence → transcribe → Hermes → speak.

Launched by HermesKey.app on a long press of the Globe/fn key. Publishes its progress to
state.json so the HermesKey HUD can animate (listening level / thinking / speaking).
A second long press while a run is active stops it (cancel recording / stop speech).
"""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import actions  # noqa: E402
from config import CFG, DATA  # noqa: E402

BASE = DATA
BASE.mkdir(parents=True, exist_ok=True)
LOCK = BASE / "ask.lock"
PIDF = BASE / "ask.pid"
STATE = BASE / "state.json"
CONV = BASE / "conv.open"
NOTIFY = BASE / "notify.json"
SESSION = BASE / "session.json"
LOG = BASE / "ask.log"
LOG_MAX_BYTES = 2_000_000   # ask.log is renamed to ask.log.1 past this size
CONTINUE_WINDOW = int(CFG["idle_reset_minutes"]) * 60   # follow-ups continue the same conversation
NO_SPEECH_TIMEOUT = 8       # give up if nothing is said
MAX_RECORD = 60
LEVEL_FULL_SCALE = 2500.0   # mic RMS that maps to a full HUD waveform
VOICE_MODEL = CFG.get("model") or ""          # "" = your Hermes default model
VOICE_PROVIDER = CFG.get("provider") or ""
HERMES = CFG["hermes_bin"]   # resolved by config.load(): config, PATH, then the usual Homebrew spots


def log(msg):
    try:
        if LOG.stat().st_size > LOG_MAX_BYTES:
            os.replace(LOG, LOG.with_name(LOG.name + ".1"))
    except OSError:
        pass
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def set_state(state, level=0.0, text=""):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"state": state, "level": round(level, 3), "text": text[:3000],
                               "ts": time.time()}))
    os.replace(tmp, STATE)   # atomic: the HUD never reads a half-written file


def acquire_lock():
    fh = open(LOCK, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Already running → this press means "stop".
        try:
            pid = int(PIDF.read_text().strip())
            os.killpg(pid, signal.SIGTERM)
            log(f"stop requested; signalled {pid}")
        except Exception as e:
            log(f"stop request failed: {e}")
        sys.exit(0)
    PIDF.write_text(str(os.getpid()))
    return fh


VOICE_PREFIX = ("[Voice input — the user is speaking to you via a push-to-talk key on their Mac and "
                "will HEAR your reply through text-to-speech. ALWAYS reply in the SAME language the "
                "user used in this message. "
                "Keep it short and concise: 1-2 sentences, straight to the point, conversational. "
                "No markdown, no lists, no code blocks, no URLs, no technical/status notes unless asked. "
                "To see anything on the Mac screen or in an app, use the computer_use tool (capture, "
                "list_windows; load it via tool_search/tool_describe if needed) — screen access is "
                "already granted through Cua Driver, so "
                "never ask the user to change macOS permissions. You may look at the screen, read apps and "
                "search the internet (web_search / web_extract), but NEVER click, type or change things "
                "yourself with tools. Instead, the bubble can do these actions for you:\n" + actions.ACTION_DOC
                + "\nFor anything else that would change the Mac, say it must be done from their usual Hermes chat or Terminal.] ")


LANG_NAMES = {"fr": "French", "en": "English", "ar": "Arabic", "es": "Spanish", "de": "German",
              "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "tr": "Turkish", "ru": "Russian",
              "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "hi": "Hindi", "pl": "Polish"}


def lang_hint():
    name = LANG_NAMES.get(LAST_LANG, LAST_LANG)
    return f"[The user just spoke {name} — answer in {name}.] "


def ask_hermes(text):
    prefix = VOICE_PREFIX + lang_hint()
    cmd = [HERMES, "chat", "-Q", "--no-restore-cwd"]
    if VOICE_PROVIDER:
        cmd += ["--provider", VOICE_PROVIDER]
    if VOICE_MODEL:
        cmd += ["-m", VOICE_MODEL]
    cmd += ["-q", prefix + text]
    resume = []
    try:
        s = json.loads(SESSION.read_text())
        if time.time() - s.get("ts", 0) < CONTINUE_WINDOW and s.get("id"):
            resume = ["--resume", s["id"]]
    except Exception:
        pass
    p = subprocess.run(cmd + resume, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    if p.returncode != 0 and resume and "SESSION_NOT_OWNED" in (p.stderr or "") + (p.stdout or ""):
        # The conversation is open in a Terminal window (notification click) — that window owns
        # it now, so the bubble carries on in a fresh session instead of failing.
        log("session owned by a Terminal window; starting a new voice session")
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    out = p.stdout or ""
    m = re.search(r"^session_id:\s*(\S+)\s*$", (p.stderr or "") + "\n" + out, re.M)
    if m:
        SESSION.write_text(json.dumps({"id": m.group(1), "ts": time.time()}))
        out = re.sub(r"^session_id:\s*\S+\s*$", "", out, flags=re.M)
    # Drop CLI housekeeping lines (cwd-restore notices etc.) so they're never spoken.
    out = "\n".join(line for line in out.splitlines()
                    if not re.match(r"^\s*(↪|⚠)\s", line) and "restored workspace dir" not in line)
    out = out.strip()
    if p.returncode != 0 and not out:
        log(f"hermes rc={p.returncode} stderr={p.stderr[-500:]}")
        return "Sorry, something went wrong talking to Hermes."
    return out or "I didn't get an answer, sorry."


SILENCE_END_SECONDS = float(CFG["silence_seconds"])   # pause that ends your question
VOICE_LANGS = tuple(CFG["languages"]) or ("en",)   # auto-detect is limited to these
LAST_LANG = VOICE_LANGS[0]                            # first configured language = fallback
VOICE_STT_MODEL = CFG["stt_model"]


def _preload_stt():
    try:
        from tools import transcription_tools as tt
        tt._get_or_load_local_model(VOICE_STT_MODEL, tt._load_stt_config().get("local") or {})
    except Exception as e:
        log(f"stt preload failed: {e}")


def transcribe(vm, wav):
    """Local Whisper with language detection restricted to VOICE_LANGS (plain auto-detect with
    a small model mislabels short French clips as Russian etc.). Falls back to Hermes' default."""
    try:
        from faster_whisper import decode_audio
        from tools import transcription_tools as tt
        model = tt._get_or_load_local_model(VOICE_STT_MODEL, tt._load_stt_config().get("local") or {})
        # Detect on the first 10 s only (fast), restricted to the user's languages.
        audio = decode_audio(wav, sampling_rate=16000)[: 16000 * 10]
        _, _, probs = model.detect_language(audio)
        scores = dict(probs or [])
        lang = max(VOICE_LANGS, key=lambda code: scores.get(code, 0.0))
        res = tt._transcribe_local(wav, VOICE_STT_MODEL, language=lang)
        text = (res.get("transcript") or "").strip()
        if res.get("success") and text and not vm.is_whisper_hallucination(text):
            log(f"stt lang={lang} ({scores.get(lang, 0):.2f})")
            global LAST_LANG
            LAST_LANG = lang
            return text
        if res.get("success"):
            return ""
        log(f"restricted stt failed: {res.get('error')}")
    except Exception as e:
        log(f"restricted stt error: {e}")
    tr = vm.transcribe_recording(wav)
    return (tr.get("transcript") or "").strip()


def fail(vm, msg):
    """Show a short error on the HUD with a low beep, then hide."""
    set_state("error", text=msg)
    vm.play_beep(440, 0.12, 1)
    time.sleep(1.8)


def conv_open():
    """HermesKey creates conv.open when the bubble opens and deletes it when ✕ is clicked."""
    return CONV.exists()


def post_notification(answer):
    """HermesKey watches notify.json and posts a clickable notification (→ Terminal)."""
    try:
        sid = json.loads(SESSION.read_text()).get("id", "")
    except Exception:
        sid = ""
    tmp = NOTIFY.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": time.time(), "text": answer[:3000], "sid": sid}))
    os.replace(tmp, NOTIFY)


# ── Voice command: "search on YouTube <thing>" → plays the first real result in the browser ──

_YT = r"(?:youtube|you tube|youtub|you-tube|iouti\s*(?:et\s*)?hoube|yout?ube?)"
_YT_PATTERNS = [
    # English
    (r"^(?:search|look\s+up|find|play|put\s+on)\s+(?:on\s+|in\s+)?" + _YT + r"\s+(?:for\s+)?(?P<q>.+)$", "en"),
    (r"^(?:search(?:\s+for)?|look\s+up|find|play|put\s+on)\s+(?P<q>.+?)\s+(?:on|in)\s+" + _YT + r"$", "en"),
    # French
    (r"^(?:cherche|recherche|mets|met|lance|joue|trouve)(?:[- ]moi)?\s+(?:sur|dans)\s+" + _YT + r"\s+(?P<q>.+)$", "fr"),
    (r"^(?:cherche|recherche|mets|met|lance|joue|trouve)(?:[- ]moi)?\s+(?P<q>.+?)\s+(?:sur|dans)\s+" + _YT + r"$", "fr"),
]


def parse_youtube_command(text):
    """Return (query, lang) if the utterance is a 'search on YouTube …' command, else None."""
    t = text.strip().strip(".!?…").strip()
    t = re.sub(r"^(?:hey|hé|salut|ok)?\s*herm[eè]s[\s,!.:]*", "", t, flags=re.I).strip()
    for pat, lang in _YT_PATTERNS:
        m = re.match(pat, t, flags=re.I)
        if m:
            q = m.group("q").strip(" ,.:;\"'«»")
            q = re.sub(r"^(?:for|pour|la|le|les|the)\s+(?=\S)", "", q, flags=re.I) if len(q.split()) > 1 else q
            if q:
                return q, lang
    return None


def youtube_first_video(query):
    """First organic (non-ad, non-Short) video on YouTube search results → (video_id, title)."""
    import urllib.parse
    import urllib.request
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
    html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html)
    if not m:
        return None
    data = json.loads(m.group(1))
    found = []

    def walk(o):
        if found:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("adSlotRenderer", "promotedVideoRenderer", "searchPyvRenderer",
                         "promotedSparklesWebRenderer", "reelShelfRenderer", "shortsLockupViewModel"):
                    continue          # ads and Shorts
                if k == "videoRenderer" and isinstance(v, dict) and v.get("videoId"):
                    badges = json.dumps(v.get("badges", []))
                    if "BADGE_STYLE_TYPE_AD" in badges or "upcoming" in json.dumps(v.get("upcomingEventData", "")):
                        continue
                    title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
                    found.append((v["videoId"], title))
                    return
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(data)
    if not found:   # newer layouts: lockupViewModel cards (contentId) — skip ad/short ones
        for m2 in re.finditer(r'"lockupViewModel":\{"contentImage".{0,4000}?"contentId":"([\w-]{11})"', html):
            found.append((m2.group(1), ""))
            break
    return found[0] if found else None


def run_youtube_command(vm, tts, cancelled, query, lang):
    set_state("thinking", text=("YouTube : " if lang == "fr" else "YouTube: ") + query)
    msg = youtube_play(query, lang)
    post_notification(msg)
    speak(vm, tts, msg, cancelled)
    return msg


def youtube_play(query, lang):
    """Open the first non-ad YouTube result for `query` in the browser; returns a sentence."""
    hit = None
    for attempt in range(2):          # YouTube occasionally serves a layout without results
        try:
            hit = youtube_first_video(query)
        except Exception as e:
            log(f"youtube search failed: {e}")
        if hit:
            break
    if not hit:
        # Fall back to showing the results page so the user can pick.
        import urllib.parse
        actions.play_url("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query))
        return (f"J'ai ouvert les résultats YouTube pour {query}." if lang == "fr"
                else f"I opened the YouTube results for {query}.")
    vid, title = hit
    title = title or query
    spoken_title = re.sub(r"#\S+", "", title)
    spoken_title = re.sub(r"[^\w\s'’\-–:,.()&!?]", "", spoken_title).strip(" -–:,") or query
    if len(spoken_title) > 80:
        spoken_title = spoken_title[:80].rsplit(" ", 1)[0]
    actions.play_url(f"https://www.youtube.com/watch?v={vid}")
    log(f"youtube: '{query}' → {vid} {title}")
    return (f"Je lance {spoken_title} sur YouTube." if lang == "fr"
            else f"Playing {spoken_title} on YouTube.")


def action_ctx(vm, tts):
    def say_later(msg):
        set_state("speaking", text=msg)
        speak(vm, tts, msg, threading.Event())
        set_state("ready" if conv_open() else "idle")
    return {"youtube_search": youtube_play, "notify": post_notification, "say": say_later}


def run(vm, tts, cancelled, followup=False):
    """One voice turn. Returns the answer text, "" when nothing was said, or None on error."""
    if os.environ.get("ASK_TEXT") and not followup:   # test hook: skip the mic
        text = os.environ["ASK_TEXT"]
        set_state("thinking", text=text)
        answer = ask_hermes(text)
        log(f"test answer: {answer[:300]}")
        post_notification(answer)
        speak(vm, tts, answer, cancelled)
        return answer
    if os.environ.get("ASK_TEXT"):
        return ""

    rec = vm.create_audio_recorder()
    if hasattr(rec, "_silence_duration"):
        rec._silence_duration = SILENCE_END_SECONDS
    # Load the Whisper model while the user is still talking, so transcription starts warm.
    threading.Thread(target=_preload_stt, daemon=True).start()
    done = threading.Event()
    set_state("listening")
    vm.play_beep(880, 0.1, 1)
    rec.start(on_silence_stop=done.set)
    t0 = time.monotonic()
    try:
        while not done.is_set() and not cancelled.is_set():
            el = time.monotonic() - t0
            if el > MAX_RECORD:
                break
            if el > NO_SPEECH_TIMEOUT and getattr(rec, "_peak_rms", 1) < getattr(rec, "_silence_threshold", 0):
                log("no speech heard")
                rec.cancel()
                if followup:
                    return ""        # quiet follow-up window: just wait in the open bubble
                fail(vm, "I didn't hear anything")
                return None
            set_state("listening", level=min(1.0, rec.current_rms / LEVEL_FULL_SCALE))
            time.sleep(0.04)
        if cancelled.is_set():
            rec.cancel()
            return
        wav = rec.stop()
    finally:
        rec.shutdown()
    vm.play_beep(660, 0.08, 2)
    if not wav:
        log("recording empty/too quiet")
        fail(vm, "I didn't hear anything")
        return

    set_state("thinking", text="…")
    t_stt = time.monotonic()
    text = transcribe(vm, wav)
    log(f"timing: stt {time.monotonic() - t_stt:.1f}s")
    try:
        os.unlink(wav)
    except OSError:
        pass
    if not text:
        log("empty transcript")
        fail(vm, "Sorry, I didn't catch that")
        return
    log(f"heard: {text}")
    lang = LAST_LANG
    yt = parse_youtube_command(text)
    if yt:
        return run_youtube_command(vm, tts, cancelled, *yt)
    ctx = action_ctx(vm, tts)
    quick = actions.fast_route(text, lang, ctx)
    if quick:
        log(f"fast action → {quick}")
        set_state("thinking", text=text)
        speak(vm, tts, quick, cancelled)
        return quick
    set_state("thinking", text=text)

    vm.start_thinking_sound(lambda: not cancelled.is_set())
    t_llm = time.monotonic()
    try:
        answer = ask_hermes(text)
    finally:
        vm.stop_thinking_sound()
    log(f"timing: hermes {time.monotonic() - t_llm:.1f}s")
    if cancelled.is_set():
        return
    acts, rest = actions.extract_actions(answer)
    if acts:
        msgs = [m for m in (actions.run_action(x, lang, ctx) for x in acts) if m]
        log(f"actions {acts} → {msgs}")
        answer = " ".join(msgs) or rest or answer
    log(f"answer: {answer[:300]}")
    post_notification(answer)
    speak(vm, tts, answer, cancelled)
    return answer


def _split_sentences(text, max_chunk=220):
    """Chunks for pipelined TTS: the FIRST sentence alone (so audio starts fast), then the rest
    merged into chunks of up to max_chunk characters, synthesized while earlier ones play."""
    parts = [p for p in re.split(r"(?<=[.!?…])\s+", text.strip()) if p]
    if not parts:
        return [text]
    chunks, cur = [parts[0]], ""
    for p in parts[1:]:
        if cur and len(cur) + len(p) + 1 > max_chunk:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return chunks


TTS_CHUNK_TIMEOUT = 3.5   # online voice slower than this → speak that chunk with macOS `say`
TTS_FIRST_CHUNK_TIMEOUT = 5.0   # the first request also opens the connection, give it a bit longer

# Native Edge voice per language. A single "multilingual" English voice reads French or Arabic
# with an English accent, so the bubble picks a voice that matches the language it heard.
EDGE_VOICES = {
    "en": "en-US-AndrewMultilingualNeural", "fr": "fr-FR-DeniseNeural", "ar": "ar-MA-JamalNeural",
    "es": "es-ES-AlvaroNeural", "de": "de-DE-ConradNeural", "it": "it-IT-DiegoNeural",
    "pt": "pt-BR-AntonioNeural", "nl": "nl-NL-MaartenNeural", "tr": "tr-TR-AhmetNeural",
    "ru": "ru-RU-DmitryNeural", "ja": "ja-JP-KeitaNeural", "zh": "zh-CN-YunxiNeural",
    "hi": "hi-IN-MadhurNeural", "ko": "ko-KR-InJoonNeural", "pl": "pl-PL-MarekNeural",
}
_ARABIC = re.compile(r"[\u0600-\u06FF]")


def spoken_lang(text):
    """Language to speak `text` in: Arabic script wins, otherwise the language the user just spoke."""
    if _ARABIC.search(text or "") and len(_ARABIC.findall(text)) > len(text) / 4:
        return "ar"
    return LAST_LANG


def edge_voice(lang):
    """Edge voice for `lang`, or None to let Hermes' own TTS settings decide."""
    try:
        from hermes_cli.config import load_config
        tts_cfg = load_config().get("tts") or {}
    except Exception:
        tts_cfg = {}
    if (tts_cfg.get("provider") or "edge").lower() != "edge":
        return None   # the user picked another TTS provider in Hermes; respect it
    overrides = CFG.get("tts_voices") or {}
    return overrides.get(lang) or EDGE_VOICES.get(lang)


_SAY_VOICES = {}


def say_voice(lang):
    """Best installed macOS voice for `lang` (Premium > Enhanced > standard), or None."""
    if lang in _SAY_VOICES:
        return _SAY_VOICES[lang]
    best, rank = None, -1
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s+([a-z]{2,3})_(\w+)\s+#", line)
        if not m or m.group(2) != lang:
            continue
        name = m.group(1).strip()
        r = (2 if "Premium" in name else 1 if "Enhanced" in name else 0) * 10
        r += 5 if m.group(3) in ("FR", "US", "001", "ES", "DE", "IT", "BR") else 0
        r -= 20 if re.search(r"Eddy|Flo|Grand|Reed|Rocko|Sandy|Shelley", name) else 0   # novelty-ish voices
        if r > rank:
            best, rank = name, r
    _SAY_VOICES[lang] = best
    return best


def _edge_synth(text, out, voice):
    import asyncio
    import edge_tts
    asyncio.run(edge_tts.Communicate(text, voice=voice).save(out))
    if not os.path.getsize(out):
        raise RuntimeError("empty audio")
    return {"success": True, "file_path": out}


def say_fallback(text, lang):
    v = say_voice(lang)
    subprocess.run(["say", "-v", v, text] if v else ["say", text])


def speak(vm, tts, text, cancelled):
    shown = text
    try:
        from tools.tts_text_normalize import prepare_spoken_text
        text = prepare_spoken_text(text, max_chars=None)
    except Exception:
        pass
    import queue
    chunks = _split_sentences(text)
    lang = spoken_lang(text)
    voice = edge_voice(lang)
    q = queue.Queue()
    t0 = time.monotonic()

    def synth():
        for i, chunk in enumerate(chunks):
            if cancelled.is_set():
                break
            out = str(BASE / f"tts_{os.getpid()}_{i}.mp3")
            box = {}
            def job(chunk=chunk, out=out, box=box):
                try:
                    if voice:
                        try:
                            box["r"] = _edge_synth(chunk, out, voice)
                            return
                        except Exception as e:   # bad/retired voice name → Hermes' configured voice
                            log(f"edge voice {voice} failed ({str(e)[:80]}); trying Hermes TTS")
                    box["r"] = json.loads(tts(text=chunk, output_path=out))
                except Exception as e:
                    box["r"] = {"success": False, "error": str(e)}
            limit = TTS_FIRST_CHUNK_TIMEOUT if i == 0 else TTS_CHUNK_TIMEOUT
            th = threading.Thread(target=job, daemon=True)
            th.start()
            th.join(limit)
            r = box.get("r") or {"success": False, "error": f"online voice slower than {limit}s"}
            q.put((chunk, r, out))
        q.put(None)

    threading.Thread(target=synth, daemon=True).start()
    first = True
    while True:
        item = q.get()
        if item is None or cancelled.is_set():
            break
        chunk, r, out = item
        if first:
            log(f"timing: tts first audio {time.monotonic() - t0:.1f}s ({len(chunks)} chunk(s), "
                f"lang={lang}, voice={voice or 'hermes default'})")
            set_state("speaking", text=shown)
            first = False
        paths = r.get("file_paths") or [r.get("file_path") or out]
        if r.get("success"):
            for p in paths:
                if cancelled.is_set():
                    break
                if os.path.isfile(p):
                    vm.play_audio_file(p)
        else:
            # Edge TTS is an online service and occasionally returns no audio — fall back to
            # the Mac's built-in voice so the answer is still spoken.
            log(f"tts failed ({str(r.get('error'))[:120]}); using macOS say")
            if not cancelled.is_set():
                say_fallback(chunk, lang)
        for p in set(paths + [out, out.rsplit(".", 1)[0] + ".ogg"]):
            try:
                os.unlink(p)
            except OSError:
                pass


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    _lock = acquire_lock()
    try:
        os.setpgrp()   # own process group so a "stop" press also kills hermes/afplay children
    except OSError:
        pass
    PIDF.write_text(str(os.getpgrp()))

    from tools import voice_mode as vm
    from tools.tts_tool import text_to_speech_tool

    cancelled = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: (cancelled.set(), vm.stop_playback()))
    try:
        answer = run(vm, text_to_speech_tool, cancelled)
        # Conversation mode: while the bubble is open, keep listening for follow-ups after
        # each answer. A quiet follow-up window just leaves the bubble open ("ready").
        while answer and conv_open() and not cancelled.is_set():
            answer = run(vm, text_to_speech_tool, cancelled, followup=True)
    finally:
        set_state("ready" if conv_open() else "idle")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        log("crash: " + traceback.format_exc())
        try:
            set_state("idle")
        except Exception:
            pass
