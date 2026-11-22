"""Shared configuration for the Hermes Bubble runtime.

Everything user-specific lives in $HERMES_HOME/bubble/config.json (created by
`hermes bubble install`). Nothing personal is hardcoded in the code.
"""
import json
import os
import shutil
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
DATA = Path(os.environ.get("HERMES_BUBBLE_HOME") or (HERMES_HOME / "bubble"))
RUNTIME = Path(__file__).resolve().parent

DEFAULTS = {
    # Model used for bubble conversations. Empty = your Hermes default model.
    # A small fast model (e.g. claude-haiku-*, gpt-*-mini) makes the bubble feel snappier.
    "model": "",
    "provider": "",
    # Languages you speak to the bubble. Speech recognition auto-detects among these only
    # (much more reliable than open auto-detect on short clips). First one = fallback.
    "languages": ["en", "fr", "es", "de", "it", "pt", "ar"],
    "stt_model": "small",          # faster-whisper model: tiny | base | small | medium
    "silence_seconds": 1.2,        # pause that ends your question
    "hold_seconds": 0.45,          # how long to hold 🌐 to trigger the bubble
    "browser_app": "Google Chrome",
    "browser_profile": "Default",  # Chrome profile directory used for searches/YouTube/history
    "api_url": "http://127.0.0.1:8642/v1/chat/completions",
    "max_turns": 30,               # a voice conversation restarts after this many turns
    "idle_reset_minutes": 10,      # ...or after this long without talking
    "hermes_bin": "",              # filled in by the installer
}


def load():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((DATA / "config.json").read_text()))
    except (OSError, ValueError):
        pass
    if not cfg.get("hermes_bin") or not os.path.exists(cfg["hermes_bin"]):
        cfg["hermes_bin"] = shutil.which("hermes") or "hermes"
    return cfg


def save(cfg):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


CFG = load()


def cua_driver():
    """Path to Cua Driver (installed by Hermes' computer_use toolset), or None."""
    for d in sorted((HERMES_HOME / "tools").glob("cua-driver-*"), reverse=True):
        p = d / "CuaDriver.app/Contents/MacOS/cua-driver"
        if p.exists():
            return str(p)
    return shutil.which("cua-driver")
