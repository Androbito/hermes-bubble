"""Routing tests — run with:  python3 -m pytest tests  (no Mac apps are touched)."""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
os.environ.setdefault("HERMES_BUBBLE_HOME", str(ROOT / ".test-data"))

import actions  # noqa: E402


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
    for i, (p, _) in enumerate(actions._FAST):
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
