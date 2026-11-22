"""Routing tests — run with:  python3 -m pytest tests  (no Mac apps are touched)."""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
TEST_DATA = ROOT / ".test-data"
os.environ.setdefault("HERMES_BUBBLE_HOME", str(TEST_DATA))
TEST_DATA.mkdir(exist_ok=True)
(TEST_DATA / "config.json").write_text(json.dumps({"languages": ["es", "en"]}))

import actions  # noqa: E402
import ask  # noqa: E402


def route(text):
    """Which fast rule a phrase hits, without executing it."""
    n = actions._fast_norm(text)
    for i, (p, _) in enumerate(actions._SPOTIFY_FAST):
        if re.match(p, n, re.I):
            return ("spotify", i)
    for i, (p, _) in enumerate(actions._FAST_MORE):
        if re.match(p, n, re.I):
            return ("more", i)
    s = actions._strip_player(n)
    for i, (p, *_) in enumerate(actions._FAST):
        if re.match(p, s, re.I):
            return ("media", i)
    return None


def test_media_commands():
    for t in ["Pause.", "Mets pause", "Arrête la musique", "Stop the music"]:
        assert route(t) == ("media", 0), t
    for t in ["Reprends", "Play", "Resume"]:
        assert route(t) == ("media", 1), t
    for t in ["Suivant", "Chanson suivante", "Next song", "Skip this song", "Next on Spotify"]:
        assert route(t) == ("media", 2), t


def test_spotify_commands():
    assert route("Ouvre ma bibliothèque et joue au hasard") == ("spotify", 0)
    assert route("Play my library on shuffle") == ("spotify", 0)
    assert route("Mets Linkin Park sur Spotify") == ("spotify", 1)
    assert route("Search Spotify for jazz piano") == ("spotify", 1)


def test_app_and_web_commands():
    assert route("Ouvre un nouveau document BBEdit") == ("more", 0)
    assert route("Cherche la météo à Paris sur Google") == ("more", 1)
    assert route("Appelle Sarah sur WhatsApp") == ("more", 2)
    assert route("Ouvre Calculatrice") == ("more", 3)


def test_questions_are_not_commands():
    for t in ["Quelle heure est-il ?", "What is YouTube?", "Explain quantum computing"]:
        assert route(t) is None, t


def test_action_extraction():
    acts, rest = actions.extract_actions('ACTION {"do":"open_app","name":"Notes"}')
    assert acts == [{"do": "open_app", "name": "Notes"}] and rest == ""


def test_contact_matching_ignores_non_chat_buttons():
    els = [
        {"role": "AXButton", "label": "Follow \u201cSarah Fans\u201d", "value": ""},
        {"role": "AXButton", "label": "Sarah", "value": "\u200eYour message, hi, 12:05"},
    ]
    btn, score = actions._contact_button(els, "sarah")
    assert btn["label"] == "Sarah" and score == 1.0


def test_youtube_command_parsing():
    import ask
    assert ask.parse_youtube_command("Search on YouTube lofi hip hop") == ("lofi hip hop", "en")
    assert ask.parse_youtube_command("Cherche sur YouTube Stromae") == ("Stromae", "fr")
    assert ask.parse_youtube_command("What is YouTube?") is None


def test_transcription_languages_follow_config():
    assert ask.VOICE_LANGS == ("es", "en")
    assert ask.LAST_LANG == "es"


def test_fast_route_strips_player_and_keeps_command(monkeypatch):
    calls = []
    monkeypatch.setattr(actions, "any_media", lambda cmd, lang, text="": calls.append((cmd, lang, text)) or "ok")
    assert actions.fast_route("Pause sur Spotify", "fr", {}) == "ok"
    assert calls == [("pause", "fr", "pause sur spotify")]


def test_fast_norm_drops_wake_word_and_politeness():
    assert actions._fast_norm("Hé Hermès, mets pause s'il te plaît.") == "mets pause"


def test_action_extraction_keeps_malformed_action_lines():
    acts, rest = actions.extract_actions("ACTION {not json}\nHello")
    assert acts == [] and rest == "ACTION {not json}\nHello"


def test_every_documented_action_has_a_handler():
    documented = set(re.findall(r'"do":"(\w+)"', actions.ACTION_DOC))
    assert documented == set(actions.ACTIONS)


def test_timer_forgets_itself_after_ringing(monkeypatch):
    monkeypatch.setattr(actions.subprocess, "run", lambda *a, **k: None)   # no afplay
    rung = []
    assert actions.timer(1, "tea", "en", rung.append, rung.append) == "Timer set for 1 s."
    t = actions._timers[-1]
    t.join(3)
    assert rung == ["Your tea timer is done."] * 2
    assert t not in actions._timers


def test_youtube_command_parsing_french_forms():
    assert ask.parse_youtube_command("Mets-moi Fairuz sur YouTube") == ("Fairuz", "fr")
    assert ask.parse_youtube_command("Cherche sur YouTube la vidéo de Daft Punk") == ("vidéo de Daft Punk", "fr")


def test_runtime_command_parsing():
    import config
    cmd = ["/x/.venv/bin/python", "-I", "-c", "import sys; sys.path.insert(0, '/x/src'); import hermes_cli"]
    assert config.parse_runtime_command(cmd) == ("/x/.venv/bin/python", "/x/src")
    assert config.parse_runtime_command(["python"]) == ("python", "")


def test_log_rotates_when_too_big(tmp_path, monkeypatch):
    monkeypatch.setattr(ask, "LOG", tmp_path / "ask.log")
    monkeypatch.setattr(ask, "LOG_MAX_BYTES", 100)
    (tmp_path / "ask.log").write_text("x" * 101)
    ask.log("new line")
    assert (tmp_path / "ask.log.1").read_text() == "x" * 101
    assert "new line" in (tmp_path / "ask.log").read_text()


def test_hermes_bin_falls_back_to_known_locations(tmp_path, monkeypatch):
    import config
    fake = tmp_path / "hermes"
    fake.write_text("")
    monkeypatch.setattr(config, "HERMES_CANDIDATES", (str(fake),))
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    assert config.load()["hermes_bin"] == str(fake)
