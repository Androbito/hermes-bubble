"""Bubble actions — the "Google Assistant" side of the Hermes 🌐 bubble.

Two ways in:
  1. fast_route(text, lang): regex fast path for very common commands (no LLM, instant)
  2. ACTION lines: for everything else Hermes (Haiku) answers with `ACTION {json}` when the user
     wants one of the actions below; run_actions() executes them.

Every action returns a short sentence to speak, in the user's language.
App control goes through Cua Driver (installed by Hermes' computer_use toolset; it holds the
Accessibility + Screen Recording grants), plain `open`, or osascript — never the agent's own tools.
YouTube runs in your browser (Chrome by default); Spotify uses the desktop app.
"""
import difflib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

from config import CFG, DATA, cua_driver

HOME = Path.home()
CUA = cua_driver() or ""
BROWSER_APP = CFG.get("browser_app") or "Google Chrome"          # process / app name
CHROME_BIN = f"/Applications/{BROWSER_APP}.app/Contents/MacOS/{BROWSER_APP}"
CHROME_PROFILE = CFG.get("browser_profile") or "Default"
CHROME_HISTORY = HOME / "Library/Application Support/Google/Chrome" / CHROME_PROFILE / "History"
APP_DIRS = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
            str(HOME / "Applications"), "/Applications/Utilities"]

_log = print


def set_logger(fn):
    global _log
    _log = fn


def T(lang, fr, en):
    return fr if lang == "fr" else en


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", " ", s.replace("\u200e", "")).casefold().strip()


# ── Cua Driver helpers ───────────────────────────────────────────────────────

def cua(tool, args, timeout=20):
    if not CUA:
        _log("Cua Driver not found — enable Hermes' computer_use toolset (`hermes tools`)")
        return {}
    p = subprocess.run([CUA, "call", tool, json.dumps(args)], capture_output=True, text=True,
                       timeout=timeout)
    out = p.stdout.strip()
    try:
        return json.loads(out[out.index("{"):]) if "{" in out else {}
    except ValueError:
        return {"raw": out[:500]}


def pid_of(app):
    p = subprocess.run(["pgrep", "-x", app], capture_output=True, text=True)
    return int(p.stdout.split()[0]) if p.stdout.strip() else None


def main_window(pid, prefer=None):
    """Largest on-screen window of pid (optionally whose title contains `prefer`); falls back to
    an off-screen window with a matching title (e.g. a window on another Space)."""
    allw = [w for w in cua("list_windows", {"pid": pid}).get("windows", []) if w["bounds"]["width"] > 400]
    wins = [w for w in allw if w.get("is_on_screen")]
    if prefer and not any(prefer.casefold() in (w.get("title") or "").casefold() for w in wins):
        wins = [w for w in allw if prefer.casefold() in (w.get("title") or "").casefold()] or wins
    if prefer:
        pref = [w for w in wins if prefer.casefold() in (w.get("title") or "").casefold()]
        wins = pref or wins
    wins.sort(key=lambda w: w["bounds"]["width"] * w["bounds"]["height"], reverse=True)
    return wins[0]["window_id"] if wins else None


def elements(pid, wid):
    return cua("get_window_state", {"pid": pid, "window_id": wid}, timeout=30).get("elements", [])


def click(pid, token):
    r = cua("click", {"pid": pid, "element_token": token})
    return "refusal" not in r


# ── Media (YouTube in the browser) ───────────────────────────────────────────

_PLAY_KEY = re.compile(r"(\(k\)|(raccourci clavier|keyboard shortcut)\s+k\b|\bk\)?$)", re.I)


def _is_playpause(e):
    lab = e.get("label") or ""
    return e.get("role") == "AXButton" and re.match(r"^(pause|lire|play|lecture)\b", lab, re.I) and _PLAY_KEY.search(lab)


def _is_playing_btn(e):
    return _is_playpause(e) and (e.get("label") or "").lower().startswith("pause")


def _fs_player():
    """Browser window whose active tab is YouTube — the one currently playing if any."""
    pid = pid_of(BROWSER_APP)
    if not pid:
        return None, None, []
    wins = [w for w in cua("list_windows", {"pid": pid}).get("windows", [])
            if "youtube" in (w.get("title") or "").casefold() and w["bounds"]["width"] > 400]
    wins.sort(key=lambda w: not w.get("is_on_screen"))
    fallback = None
    for w in wins:
        els = elements(pid, w["window_id"])
        play = next((e for e in els if _is_playpause(e)), None)
        if play and _is_playing_btn(play):
            return pid, w["window_id"], els          # this one is playing
        if play and not fallback:
            fallback = (pid, w["window_id"], els)
    return fallback or (pid, None, [])


def _next_recommendation(els):
    """First 'up next' recommendation card on a watch page (heading + link with the same title)."""
    heads = {(e.get("label") or "").strip() for e in els if e.get("role") == "AXHeading"}
    for e in els:
        lab = (e.get("label") or "").strip()
        if e.get("role") == "AXLink" and len(lab) > 12 and any(h and lab.startswith(h[:30]) for h in heads):
            return e
    return None


def media(cmd, lang):
    """cmd: pause | play | toggle | next | previous"""
    _set_last("youtube")
    if cmd in ("play", "toggle"):
        pause_spotify_if_playing()
    pid, wid, els = _fs_player()
    if not els:
        return T(lang, "Aucune vidéo YouTube ouverte dans le navigateur.", "No YouTube video is open in the browser.")
    def btn(pat):
        return next((e for e in els if e.get("role") == "AXButton"
                     and re.search(pat, e.get("label") or "", re.I)), None)
    if cmd in ("pause", "play", "toggle"):
        b = next((e for e in els if _is_playpause(e)), None)
        if not b:
            return T(lang, "Je ne trouve pas le lecteur.", "I can't find the player.")
        is_playing = (b.get("label") or "").lower().startswith("pause")
        if (cmd == "pause" and not is_playing) or (cmd == "play" and is_playing):
            return T(lang, "C'est déjà en pause." if cmd == "pause" else "Ça joue déjà.",
                     "It's already paused." if cmd == "pause" else "It's already playing.")
        click(pid, b["element_token"])
        return T(lang, "Pause." if is_playing else "Je reprends.", "Paused." if is_playing else "Resuming.")
    if cmd == "next":
        b = btn(r"(shift|maj)\s*\+\s*n\b|^(suivant|next)\b")
        if b:                                   # playlist / autoplay "next" button
            click(pid, b["element_token"])
            return T(lang, "Vidéo suivante.", "Next video.")
        rec = _next_recommendation(els)         # otherwise: first recommended video
        if not rec:
            return T(lang, "Je ne trouve pas de vidéo suivante.", "I can't find a next video.")
        click(pid, rec["element_token"])
        return T(lang, f"Je passe à {spoken(rec.get('label'))}.", f"Next: {spoken(rec.get('label'))}.")
    if cmd == "previous":
        b = btn(r"(shift|maj)\s*\+\s*p|^(précédent|precedent|previous)\b")
        if b:
            click(pid, b["element_token"])
        else:                                   # browser "back" in that window
            cua("hotkey", {"pid": pid, "window_id": wid, "keys": ["cmd", "["]})
        return T(lang, "Vidéo précédente.", "Previous video.")
    return None


def now_playing(lang):
    if spotify_state() == "playing":
        return spotify_now_playing(lang)
    pid, wid, _ = _fs_player()
    if not pid:
        return T(lang, "Aucune vidéo YouTube ouverte.", "No YouTube video is open.")
    title = next((w.get("title") for w in cua("list_windows", {"pid": pid}).get("windows", [])
                  if w.get("window_id") == wid), "") or ""
    title = re.sub(r"^\(\d+\)\s*", "", title)
    title = re.sub(r"\s+-\s+YouTube.*$", "", title).strip()
    return T(lang, f"En ce moment : {title}.", f"Now playing: {title}.") if title else \
        T(lang, "Je ne vois rien en lecture.", "I don't see anything playing.")


# ── Spotify (desktop app, AppleScript) ───────────────────────────────────────

_LAST_FILE = DATA / "last_player"


def _last_player():
    """What the user controlled last ('spotify' | 'youtube'), persisted across worker restarts."""
    try:
        return _LAST_FILE.read_text().strip() or None
    except OSError:
        return None


def _set_last(p):
    try:
        _LAST_FILE.write_text(p)
    except OSError:
        pass



def _spotify_user():
    try:
        d = HOME / "Library/Application Support/Spotify/Users"
        names = [n[:-5] for n in os.listdir(d) if n.endswith("-user")]
        return names[0] if names else None
    except OSError:
        return None


def sp(cmd):
    """Run an AppleScript command against Spotify; returns stdout ('' on error)."""
    r = subprocess.run(["osascript", "-e", f'tell application "Spotify" to {cmd}'],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        _log(f"spotify '{cmd}': {r.stderr.strip()[:150]}")
    return r.stdout.strip()


def spotify_running():
    return bool(pid_of("Spotify"))


def spotify_state():
    return sp("get player state") if spotify_running() else "stopped"


def _spotify_launch():
    if not spotify_running():
        subprocess.run(["open", "-g", "-a", "Spotify"])
        for _ in range(30):
            if spotify_running() and sp("get player state"):
                break
            time.sleep(0.4)
        time.sleep(1.0)


def _spotify_now(lang, prefix_fr="", prefix_en=""):
    time.sleep(1.2)
    name, artist = sp("get name of current track"), sp("get artist of current track")
    if not name:
        return None
    what = f"{name}, {artist}" if artist else name
    return T(lang, f"{prefix_fr}{spoken(what)}.", f"{prefix_en}{spoken(what)}.")


def _pause_youtube_if_playing():
    try:
        pid, wid, els = _fs_player()
        b = next((e for e in els if _is_playing_btn(e)), None)
        if b:
            click(pid, b["element_token"])
    except Exception as e:
        _log(f"pause youtube failed: {e}")


def pause_spotify_if_playing():
    if spotify_running() and spotify_state() == "playing":
        sp("pause")


def spotify_media(cmd, lang):
    if not spotify_running():
        if cmd in ("play", "toggle"):
            _spotify_launch()
        else:
            return T(lang, "Spotify n'est pas ouvert.", "Spotify isn't open.")
    _set_last("spotify")
    state = spotify_state()
    if cmd == "pause":
        sp("pause")
        return T(lang, "Pause.", "Paused.")
    if cmd in ("play", "toggle"):
        if cmd == "toggle" and state == "playing":
            sp("pause")
            return T(lang, "Pause.", "Paused.")
        # Spotify reports the old state for ~1 s after a pause, so just (re)send play.
        _pause_youtube_if_playing()
        sp("play")
        return _spotify_now(lang, "Je reprends ", "Resuming ") or T(lang, "Je reprends.", "Resuming.")
    if cmd == "next":
        sp("next track")
        return _spotify_now(lang, "Titre suivant : ", "Next: ") or T(lang, "Titre suivant.", "Next track.")
    if cmd == "previous":
        sp("previous track")
        return _spotify_now(lang, "Titre précédent : ", "Previous: ") or T(lang, "Titre précédent.", "Previous track.")
    return None


def spotify_now_playing(lang):
    if spotify_state() not in ("playing", "paused"):
        return None
    r = _spotify_now(lang, "Sur Spotify : ", "On Spotify: ")
    return r


def _spotify_search_uri(query):
    """First Spotify track/album/playlist/artist for `query` (via web search of open.spotify.com)."""
    from tools.web_tools import web_search_tool
    r = json.loads(web_search_tool("site:open.spotify.com " + query, limit=6))
    for item in r.get("data", {}).get("web", []):
        m = re.search(r"open\.spotify\.com/(?:intl-\w+/)?(track|album|playlist|artist)/([A-Za-z0-9]{22})",
                      item.get("url", ""))
        if m:
            return f"spotify:{m.group(1)}:{m.group(2)}", m.group(1)
    return None, None


def spotify_search(query, lang):
    uri, kind = _spotify_search_uri(query)
    if not uri:
        _spotify_launch()
        subprocess.run(["open", "spotify:search:" + urllib.parse.quote(query)])
        return T(lang, f"J'ai ouvert la recherche Spotify pour {query}.", f"I opened the Spotify search for {query}.")
    _pause_youtube_if_playing()
    _spotify_launch()
    sp(f'play track "{uri}"')
    _set_last("spotify")
    return _spotify_now(lang, "Sur Spotify : ", "On Spotify: ") or \
        T(lang, f"Je lance {query} sur Spotify.", f"Playing {query} on Spotify.")


def spotify_library_shuffle(lang):
    user = _spotify_user()
    if not user:
        return T(lang, "Je ne trouve pas ton compte Spotify.", "I can't find your Spotify account.")
    _pause_youtube_if_playing()
    _spotify_launch()
    sp("set shuffling to true")
    sp(f'play track "spotify:user:{user}:collection"')
    time.sleep(1.0)
    sp("set shuffling to true")
    sp("next track")                     # jump to a random liked song
    _set_last("spotify")
    return _spotify_now(lang, "Tes titres likés en aléatoire. On commence par ",
                        "Your liked songs on shuffle, starting with ") or \
        T(lang, "Je lance tes titres likés en aléatoire.", "Playing your liked songs on shuffle.")


def pick_player(text=""):
    """Which player a bare 'pause / next / play' should control."""
    t = (text or "").casefold()
    if "spotify" in t:
        return "spotify"
    if "youtube" in t or "vid" in t:
        return "youtube"
    if spotify_state() == "playing":
        return "spotify"
    try:
        _, _, els = _fs_player()
        if any(_is_playing_btn(e) for e in els):
            return "youtube"
    except Exception:
        pass
    last = _last_player()
    if last:
        return last
    return "spotify" if spotify_state() == "paused" else "youtube"


def any_media(cmd, lang, text=""):
    return spotify_media(cmd, lang) if pick_player(text) == "spotify" else media(cmd, lang)


# ── YouTube: search & history ────────────────────────────────────────────────

def play_url(url):
    pause_spotify_if_playing()
    _set_last("youtube")
    chrome_open(url)


def _history_rows():
    rows = []
    tmp = Path(tempfile.mkdtemp(prefix="hk-hist-"))
    try:
        if CHROME_HISTORY.exists():
            shutil.copy(CHROME_HISTORY, tmp / "chrome")
            con = sqlite3.connect(tmp / "chrome")
            # Chrome time: microseconds since 1601-01-01
            rows += [(u, (t or "").replace(" - YouTube", ""),
                      time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(v / 1e6 - 11644473600)))
                     for u, t, v in con.execute(
                         "select url, title, last_visit_time from urls where url like "
                         "'%youtube.com/watch%' order by last_visit_time desc limit 3000")]
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows


def youtube_history(keyword, lang):
    words = [w for w in norm(keyword).split() if len(w) > 1]
    if not words:
        return T(lang, "Dis-moi quoi chercher dans l'historique.", "Tell me what to look for in your history.")
    best = None
    for url, title, when in _history_rows():
        nt = norm(title)
        hits = sum(1 for w in words if w in nt)
        if not hits:
            continue
        score = (hits / len(words), when)
        if not best or score > best[0]:
            best = (score, url, title)
    if not best or best[0][0] < 0.5:
        return T(lang, f"Je n'ai rien trouvé dans ton historique pour {keyword}.",
                 f"I found nothing in your history for {keyword}.")
    vid = re.search(r"[?&]v=([\w-]{11})", best[1])
    play_url(f"https://www.youtube.com/watch?v={vid.group(1)}" if vid else best[2])
    return T(lang, f"Je relance {spoken(best[2])} depuis ton historique.",
             f"Playing {spoken(best[2])} from your history.")


def spoken(title, limit=80):
    s = re.sub(r"#\S+", "", title or "")
    s = re.sub(r"[^\w\s'’\-–:,.()&!?]", "", s).strip(" -–:,")
    return s[:limit].rsplit(" ", 1)[0] if len(s) > limit else s


# ── Apps, documents, web ─────────────────────────────────────────────────────

ALIASES = {"chrome": "Google Chrome", "google chrome": "Google Chrome", "whatsapp": "WhatsApp", "réglages": "System Settings",
           "reglages": "System Settings", "settings": "System Settings", "terminal": "Terminal",
           "notes": "Notes", "calendrier": "Calendar", "calculatrice": "Calculator",
           "musique": "Music", "plans": "Maps", "photos": "Photos", "mail": "Mail",
           "messages": "Messages", "rappels": "Reminders", "finder": "Finder"}


def _installed_apps():
    apps = {}
    for d in APP_DIRS:
        try:
            for n in os.listdir(d):
                if n.endswith(".app"):
                    apps[n[:-4]] = os.path.join(d, n)
        except OSError:
            pass
    return apps


def find_app(name):
    n = norm(name)
    if n in ALIASES:
        n = norm(ALIASES[n])
    apps = _installed_apps()
    by_norm = {norm(k): k for k in apps}
    if n in by_norm:
        return by_norm[n], apps[by_norm[n]]
    starts = [k for nk, k in by_norm.items() if nk.startswith(n) or n in nk.split()]
    if starts:
        k = min(starts, key=len)
        return k, apps[k]
    close = difflib.get_close_matches(n, list(by_norm), n=1, cutoff=0.72)
    return (by_norm[close[0]], apps[by_norm[close[0]]]) if close else (None, None)


def open_app(name, lang):
    if norm(name) == "finder":
        subprocess.run(["open", "-a", "Finder"])
        return T(lang, "J'ouvre le Finder.", "Opening Finder.")
    app, path = find_app(name)
    if not app:
        return T(lang, f"Je ne trouve pas d'application qui s'appelle {name}.",
                 f"I can't find an app called {name}.")
    subprocess.run(["open", path])
    return T(lang, f"J'ouvre {app}.", f"Opening {app}.")


def bbedit_new(lang):
    if not pid_of("BBEdit"):
        subprocess.run(["open", "-a", "BBEdit"])
        for _ in range(30):                    # wait for a cold launch to finish
            if pid_of("BBEdit"):
                break
            time.sleep(0.3)
        time.sleep(2.0)
    r = subprocess.run(["osascript", "-e", 'tell application "BBEdit"\nactivate\n'
                        'make new text document\nend tell'], capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["open", "-a", "BBEdit"])
        _log(f"bbedit applescript failed: {r.stderr.strip()[:200]}")
    return T(lang, "Nouveau document BBEdit ouvert.", "New BBEdit document opened.")


def chrome_open(url):
    """Open url in the configured browser (Chrome profile aware); falls back to the default browser."""
    if os.path.exists(CHROME_BIN):
        subprocess.Popen([CHROME_BIN, f"--profile-directory={CHROME_PROFILE}", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.4)
        subprocess.run(["open", "-a", BROWSER_APP])
    else:
        subprocess.run(["open", url])


def chrome_search(query, lang):
    chrome_open("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
    return T(lang, f"Je cherche {query} sur Google.", f"Searching Google for {query}.")


def open_website(site, lang):
    url = site.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + re.sub(r"\s+", "", url)
    chrome_open(url)
    shown = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
    return T(lang, f"J'ouvre {shown}.", f"Opening {shown}.")


# ── System ───────────────────────────────────────────────────────────────────

def volume(cmd, lang, value=None):
    def osa(script):
        return subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout.strip()
    cur = int(osa("output volume of (get volume settings)") or 50)
    if cmd == "mute":
        osa("set volume with output muted")
        return T(lang, "Son coupé.", "Muted.")
    if cmd == "unmute":
        osa("set volume without output muted")
        return T(lang, "Son remis.", "Unmuted.")
    new = {"up": min(100, cur + 15), "down": max(0, cur - 15)}.get(cmd, value)
    if new is None:
        return None
    new = max(0, min(100, int(new)))
    osa(f"set volume output volume {new} without output muted")
    return T(lang, f"Volume à {new} pour cent.", f"Volume at {new} percent.")


_timers = []


def timer(seconds, label, lang, notify, say):
    seconds = int(seconds)
    if seconds <= 0:
        return None

    def ring():
        try:
            msg = T(lang, f"Le minuteur {label} est terminé." if label else "Le minuteur est terminé.",
                    f"Your {label} timer is done." if label else "Your timer is done.")
            try:
                subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"])
            except OSError:
                pass
            notify(msg)
            say(msg)
        finally:
            _timers.remove(t)
    t = threading.Timer(seconds, ring)
    t.daemon = True
    t.start()
    _timers.append(t)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    dur = " ".join(x for x in [f"{h} h" if h else "", f"{m} min" if m else "", f"{s} s" if s else ""] if x)
    return T(lang, f"Minuteur de {dur} lancé.", f"Timer set for {dur}.")


# ── WhatsApp calls ───────────────────────────────────────────────────────────

def _wa_window():
    subprocess.run(["open", "-a", "WhatsApp"])
    for _ in range(20):
        pid = pid_of("WhatsApp")
        if pid:
            wid = main_window(pid)
            if wid:
                return pid, wid
        time.sleep(0.5)
    return None, None


_ROW_HINT = re.compile(r"(message|sent to|received|reacted|voice call|video call|photo|missed|typing|"
                       r"\d{1,2}:\d{2}|yesterday|hier)", re.I)


def _contact_button(els, name):
    """Chat-list row (Chats tab only) whose contact name best matches the spoken name.
    Rows are AXButtons whose value carries the last-message summary; this excludes
    'Follow …' channel suggestions, tabs and toolbar buttons."""
    target = norm(name)
    best, best_score = None, 0.0
    for e in els:
        if e.get("role") != "AXButton":
            continue
        lab = norm(e.get("label") or "")
        val = (e.get("value") or "").replace("\u200e", "")
        if not lab or not _ROW_HINT.search(val) or lab.startswith(("follow", "suivre", "start ", "demarrer")):
            continue
        score = 1.0 if lab == target else difflib.SequenceMatcher(None, target, lab).ratio()
        if lab.startswith(target + " ") or target.startswith(lab + " "):
            score = max(score, 0.85)
        if score > best_score:
            best, best_score = e, score
    return (best, best_score) if best_score >= 0.8 else (None, best_score)


def _wa_tab(pid, wid, els, tab="Chats"):
    t = next((e for e in els if e.get("role") == "AXStaticText"
              and norm(e.get("label") or "").startswith(norm(tab))), None)
    if t:
        click(pid, t["element_token"])
        time.sleep(0.8)
        return elements(pid, wid)
    return els


def whatsapp_call(name, lang, video=False, dry_run=False):
    pid, wid = _wa_window()
    if not pid:
        return T(lang, "WhatsApp ne répond pas.", "WhatsApp isn't responding.")
    # Always work from the Chats tab: clicking a row in the Calls tab could dial immediately.
    els = _wa_tab(pid, wid, elements(pid, wid), "Chats")
    btn, _ = _contact_button(els, name)
    if not btn:
        field = next((e for e in els if e.get("role") in ("AXTextField", "AXSearchField")), None)
        if not field:
            return T(lang, f"Je ne trouve pas {name} dans WhatsApp.", f"I can't find {name} in WhatsApp.")
        click(pid, field["element_token"])
        cua("type_text", {"pid": pid, "element_token": field["element_token"], "text": name})
        time.sleep(1.5)
        els = elements(pid, wid)
        btn, _ = _contact_button(els, name)
        if not btn:
            cua("set_value", {"pid": pid, "element_token": field["element_token"], "value": ""})
    if not btn:
        return T(lang, f"Je ne trouve pas {name} dans WhatsApp.", f"I can't find {name} in WhatsApp.")
    who = (btn.get("label") or name).replace("\u200e", "").strip()
    click(pid, btn["element_token"])
    time.sleep(1.2)
    els = elements(pid, wid)
    kind = "video" if video else "voice"
    call_btn = next((e for e in els if e.get("role") == "AXButton"
                     and re.search(rf"start {kind} call with", e.get("label") or "", re.I)
                     and norm(who) in norm(e.get("label") or "")), None)
    if not call_btn:
        return T(lang, f"J'ai ouvert la conversation avec {who}, mais je ne trouve pas le bouton d'appel.",
                 f"I opened the chat with {who}, but can't find the call button.")
    if dry_run:
        return f"DRY RUN: would click '{(call_btn.get('label') or '').strip()}'"
    click(pid, call_btn["element_token"])
    return T(lang, f"J'appelle {who} sur WhatsApp{' en vidéo' if video else ''}.",
             f"Calling {who} on WhatsApp{' (video)' if video else ''}.")


# ── Routing ──────────────────────────────────────────────────────────────────

_FAST = [   # (pattern, media command when "… sur spotify/youtube" is stripped, handler)
    (r"^(?:mets?\s+(?:en\s+)?pause|pause|stop|arr[eê]te(?:\s+la\s+musique)?|pause\s+(?:the\s+)?(?:music|video)|"
     r"stop\s+the\s+music)$", "pause", lambda m, lang, c: any_media("pause", lang, m.string)),
    (r"^(?:play|resume|continue|reprends?|relance|joue|lecture|remets?\s+la\s+musique)$",
     "play", lambda m, lang, c: any_media("play", lang, m.string)),
    (r"^(?:next|skip|suivant(?:e)?|(?:chanson|vid[eé]o|musique)\s+suivante|passe(?:\s+[aà]\s+la\s+suivante)?|"
     r"next\s+(?:song|video|track)|skip\s+(?:this\s+)?(?:song|video|track))$",
     "next", lambda m, lang, c: any_media("next", lang, m.string)),
    (r"^(?:previous|pr[eé]c[eé]dent(?:e)?|(?:chanson|vid[eé]o)\s+pr[eé]c[eé]dente|previous\s+(?:song|video|track))$",
     "previous", lambda m, lang, c: any_media("previous", lang, m.string)),
    (r"^(?:monte|augmente)\s+(?:le\s+)?(?:son|volume)|^(?:volume|turn\s+(?:it\s+)?)\s*up|^louder$|^plus\s+fort$",
     None, lambda m, lang, c: volume("up", lang)),
    (r"^(?:baisse|diminue)\s+(?:le\s+)?(?:son|volume)|^(?:volume|turn\s+(?:it\s+)?)\s*down|^quieter$|^moins\s+fort$",
     None, lambda m, lang, c: volume("down", lang)),
    (r"^(?:coupe\s+le\s+son|mute|silence)$", None, lambda m, lang, c: volume("mute", lang)),
    (r"^(?:mets\s+)?(?:le\s+)?(?:volume|son)\s+(?:[aà]\s+)?(?P<v>\d{1,3})(?:\s*%|\s+pour\s*cent)?$|"
     r"^(?:set\s+(?:the\s+)?)?volume\s+(?:to\s+)?(?P<v2>\d{1,3})(?:\s*%|\s+percent)?$",
     None, lambda m, lang, c: volume("set", lang, int(m.group("v") or m.group("v2")))),
    (r"^(?:remets?\s+le\s+son|unmute)$", None, lambda m, lang, c: volume("unmute", lang)),
    (r"^(?:what'?s|what\s+is)\s+playing|^qu'?est[- ]ce\s+qui\s+(?:joue|passe)|^c'?est\s+quoi\s+cette\s+(?:chanson|musique)",
     None, lambda m, lang, c: now_playing(lang)),
]


def _fast_norm(text):
    t = (text or "").replace("’", "'").replace("\u200e", "").casefold().strip()
    t = re.sub(r"^(?:hey|hé|salut|ok)?\s*herm[eè]s[\s,!.:]*", "", t)
    t = re.sub(r"[.!?…,;:]+$", "", t).strip()
    t = re.sub(r"[,;]", " ", t)
    t = re.sub(r"\s+(?:s'?il te pla[iî]t|s'il vous pla[iî]t|please|stp)$", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _open_if_app(m, lang):
    name = m.group("n").strip()
    app, _ = find_app(name)
    return open_app(name, lang) if app else None


_FAST_MORE = [
    (r"^(?:(?:cr[eé]e|ouvre|fais)(?:[- ]moi)?\s+(?:un\s+)?(?:nouveau\s+)?document\s+(?:vide\s+)?(?:dans\s+|sur\s+|avec\s+)?bb\s?edit|"
     r"(?:new|create\s+(?:a\s+)?(?:new\s+)?)(?:empty\s+)?bb\s?edit\s+(?:document|doc|file)|"
     r"(?:new|create\s+(?:a\s+)?(?:new\s+)?)(?:empty\s+)?(?:document|doc|file)\s+(?:in|on)\s+bb\s?edit|"
     r"nouveau\s+document\s+bb\s?edit)$", lambda m, lang, c: bbedit_new(lang)),
    (r"^(?:cherche|recherche)\s+(?P<q>.+?)\s+(?:sur|dans|avec)\s+(?:google|chrome)$|"
     r"^(?:search|google|look\s+up)\s+(?P<q2>.+?)\s+(?:on|in|with)\s+(?:google|chrome)$|"
     r"^(?:search\s+(?:on\s+)?(?:google|chrome)\s+(?:for\s+)?|cherche\s+sur\s+(?:google|chrome)\s+|google\s+)(?P<q3>.+)$",
     lambda m, lang, c: chrome_search((m.group("q") or m.group("q2") or m.group("q3")).strip(), lang)),
    (r"^(?:appelle|t[eé]l[eé]phone\s+[aà]|call)\s+(?P<n>.+?)(?:\s+(?:sur|on|via)\s+whats\s?app)?(?P<v>\s+en\s+vid[eé]o|\s+(?:on\s+)?video)?$",
     lambda m, lang, c: whatsapp_call(m.group("n").strip(), lang, bool(m.group("v")))),
    (r"^(?:ouvre|lance|d[eé]marre|open|launch|start)\s+(?:l'application\s+|l'appli\s+|l'app\s+|the\s+app\s+|app\s+)?(?P<n>[\w .'-]{2,40})$",
     lambda m, lang, c: _open_if_app(m, lang)),
]


_SPOTIFY_FAST = [
    (r"^(?:(?:ouvre|lance|mets|joue)(?:[- ]moi)?\s+)?(?:ma\s+biblioth[eè]que|mes\s+(?:titres|chansons|sons)\s+(?:lik[eé]s|pr[eé]f[eé]r[eé]s|aim[eé]s))"
     r"(?:\s+(?:sur\s+)?spotify)?(?:\s+(?:et\s+)?(?:joue|lance|mets)?\s*(?:au\s+hasard|en\s+al[eé]atoire|en\s+shuffle|al[eé]atoirement))?$|"
     r"^(?:open|play|shuffle)\s+my\s+(?:spotify\s+)?(?:library|liked\s+songs)(?:\s+on\s+spotify)?(?:\s+(?:and\s+)?(?:play\s+)?(?:randomly|on\s+shuffle|shuffled|at\s+random))?$|"
     r"^(?:shuffle|al[eé]atoire)\s+(?:sur\s+|on\s+)?spotify$|^(?:joue|mets|lance)\s+(?:du\s+)?spotify\s+(?:au\s+hasard|en\s+al[eé]atoire)$",
     lambda m, lang, c: spotify_library_shuffle(lang)),
    (r"^(?:cherche|recherche|mets|met|lance|joue|trouve)(?:[- ]moi)?\s+(?:sur|dans)\s+spotify\s+(?P<q>.+)$|"
     r"^(?:cherche|recherche|mets|met|lance|joue|trouve)(?:[- ]moi)?\s+(?P<q2>.+?)\s+(?:sur|dans)\s+spotify$|"
     r"^(?:search|play|find|put\s+on)\s+(?:on\s+|in\s+)?spotify\s+(?:for\s+)?(?P<q3>.+)$|"
     r"^(?:search(?:\s+for)?|play|find|put\s+on)\s+(?P<q4>.+?)\s+(?:on|in)\s+spotify$",
     lambda m, lang, c: spotify_search(next(g for g in m.groups() if g).strip(" ,.'\""), lang)),
]


def _strip_player(t):
    return re.sub(r"\s+(?:sur|on|dans|in)\s+(?:spotify|youtube)$|\s+(?:spotify|youtube)$", "", t)


def fast_route(text, lang, ctx):
    t = _fast_norm(text)
    for pat, fn in _SPOTIFY_FAST:
        m = re.match(pat, t, flags=re.I)
        if m:
            return fn(m, lang, ctx)
    raw = t
    t = _strip_player(t)
    if t != raw:                      # "pause sur spotify" → match "pause", remember the player
        for pat, cmd, _ in _FAST:
            if cmd and re.match(pat, t, flags=re.I):
                return any_media(cmd, lang, raw)
        t = raw
    for pat, fn in _FAST_MORE:
        m = re.match(pat, t, flags=re.I)
        if m:
            r = fn(m, lang, ctx)
            if r:
                return r
    for pat, _, fn in _FAST:
        m = re.match(pat, t, flags=re.I)
        if m:
            return fn(m, lang, ctx)
    return None


ACTIONS = {   # do → (example JSON for the model, description, handler(action, lang, ctx))
    "media": ('{"do":"media","cmd":"pause|play|next|previous"}',
              "control whatever is playing (Spotify or YouTube)",
              lambda a, lang, ctx: any_media(a.get("cmd", "toggle"), lang)),
    "now_playing": ('{"do":"now_playing"}', "what is playing (Spotify or YouTube)",
                    lambda a, lang, ctx: now_playing(lang)),
    "spotify": ('{"do":"spotify","cmd":"pause|play|next|previous"}', "control Spotify",
                lambda a, lang, ctx: spotify_media(a.get("cmd", "toggle"), lang)),
    "spotify_search": ('{"do":"spotify_search","q":"..."}', "search Spotify and play the top result",
                       lambda a, lang, ctx: spotify_search(a.get("q", ""), lang)),
    "spotify_library": ('{"do":"spotify_library"}', "play the user's Spotify liked songs on shuffle",
                        lambda a, lang, ctx: spotify_library_shuffle(lang)),
    "youtube_search": ('{"do":"youtube_search","q":"..."}', "search YouTube and play the first non-ad video",
                       lambda a, lang, ctx: ctx["youtube_search"](a.get("q", ""), lang)),
    "youtube_history": ('{"do":"youtube_history","q":"..."}', "find a video in the user's watch history and play it",
                        lambda a, lang, ctx: youtube_history(a.get("q", ""), lang)),
    "open_app": ('{"do":"open_app","name":"..."}', "open a Mac app by name",
                 lambda a, lang, ctx: open_app(a.get("name", ""), lang)),
    "bbedit_new": ('{"do":"bbedit_new"}', "new empty BBEdit document",
                   lambda a, lang, ctx: bbedit_new(lang)),
    "web_search": ('{"do":"web_search","q":"..."}', "search Google in Chrome and show results",
                   lambda a, lang, ctx: chrome_search(a.get("q", ""), lang)),
    "open_website": ('{"do":"open_website","url":"..."}', "open a website in Chrome",
                     lambda a, lang, ctx: open_website(a.get("url", ""), lang)),
    "whatsapp_call": ('{"do":"whatsapp_call","name":"...","video":false}', "call a WhatsApp contact (voice or video)",
                      lambda a, lang, ctx: whatsapp_call(a.get("name", ""), lang, bool(a.get("video")))),
    "volume": ('{"do":"volume","cmd":"up|down|mute|unmute|set","value":0-100}', "",
               lambda a, lang, ctx: volume(a.get("cmd", "set"), lang, a.get("value"))),
    "timer": ('{"do":"timer","seconds":N,"label":"..."}', "countdown timer with a spoken alert",
              lambda a, lang, ctx: timer(a.get("seconds", 0), a.get("label", ""), lang, ctx["notify"], ctx["say"])),
}

ACTION_DOC = ("BUBBLE ACTIONS — when the user asks for one of these, do NOT use any tool; instead reply with\n"
              "exactly one line `ACTION {json}` (nothing else). Available actions:\n"
              + "\n".join(f"  {example:<58} {desc}".rstrip() for example, desc, _ in ACTIONS.values())
              + "\nOnly use ACTION when the user clearly asks for that action; questions get normal spoken answers.")


def run_action(a, lang, ctx):
    do = a.get("do")
    if do not in ACTIONS:
        return None
    try:
        return ACTIONS[do][2](a, lang, ctx)
    except Exception as e:
        _log(f"action {do} failed: {e}")
        return T(lang, "Désolé, cette action a échoué.", "Sorry, that action failed.")


def extract_actions(answer):
    """Split a Hermes reply into (actions, remaining_text)."""
    acts, keep = [], []
    for line in (answer or "").splitlines():
        m = re.match(r"^\s*`?ACTION\s*(\{.*\})`?\s*$", line)
        if m:
            try:
                acts.append(json.loads(m.group(1)))
                continue
            except ValueError:
                pass
        keep.append(line)
    return acts, "\n".join(keep).strip()
