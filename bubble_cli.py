"""`hermes bubble` — install and manage the Hermes Bubble voice assistant (macOS)."""
import argparse
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
RUNTIME = PLUGIN_DIR / "runtime"
APP_SRC = PLUGIN_DIR / "app" / "HermesKey.swift"
BUNDLE_ID = "io.github.hermes-bubble.hermeskey"
AGENT_LABEL = "io.github.hermes-bubble.hermeskey"
LAUNCH_AGENT = Path.home() / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist"

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("hermes_bubble_config", RUNTIME / "config.py")
bubble_config = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(bubble_config)

HERMES_HOME = bubble_config.HERMES_HOME
DATA = bubble_config.DATA
APP = DATA / "HermesKey.app"

DESCRIPTION = """Hold the 🌐 Globe key anywhere on your Mac, speak, and Hermes answers out loud.

  hermes bubble install      one-shot setup (voice deps, API server, app, login item)
  hermes bubble status       health check
  hermes bubble start|stop   start / stop the bubble
  hermes bubble config       show or change settings (model, languages, browser profile…)
  hermes bubble uninstall    remove the app and login item (add --purge to delete settings)
"""


# ── helpers ──────────────────────────────────────────────────────────────────

def say(msg=""):
    print(msg, flush=True)


def ok(msg):
    say(f"  ✓ {msg}")


def warn(msg):
    say(f"  ⚠ {msg}")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ask_yes(question, assume_yes):
    if assume_yes or not sys.stdin.isatty():
        return True
    ans = input(f"  ? {question} [Y/n] ").strip().lower()
    return ans in ("", "y", "yes")


def hermes_bin():
    exe = shutil.which("hermes")
    if exe:
        return exe
    cand = Path(sys.argv[0])
    return str(cand.resolve()) if cand.exists() else "hermes"


def env_file():
    return HERMES_HOME / ".env"


def env_has(key):
    try:
        return any(line.startswith(key + "=") for line in env_file().read_text().splitlines())
    except OSError:
        return False


def worker_ping():
    import socket
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2)
    try:
        s.connect(str(DATA / "worker.sock"))
        s.sendall(b"ping\n")
        return s.recv(16).startswith(b"pong")
    except OSError:
        return False
    finally:
        s.close()


def app_running():
    return bool(run(["pgrep", "-f", f"{APP}/Contents/MacOS/HermesKey"]).stdout.strip())


# ── install steps ────────────────────────────────────────────────────────────

def check_platform():
    if sys.platform != "darwin":
        raise SystemExit("Hermes Bubble is macOS-only (it uses the 🌐 Globe key, Core Audio and Accessibility).")
    if not shutil.which("swiftc"):
        raise SystemExit("swiftc not found — install Apple's command line tools first:  xcode-select --install")
    ok(f"macOS {platform.mac_ver()[0]} ({platform.machine()}), swiftc found")


def install_voice_deps():
    try:
        __import__("sounddevice"), __import__("faster_whisper")
        ok("voice dependencies already installed")
        return
    except ImportError:
        pass
    say("  … installing Hermes voice dependencies (sounddevice, faster-whisper) — this can take a minute")
    try:
        from pm.client import sync_venv
        sync_venv(["voice"], explicit=True)
        ok("voice dependencies installed")
    except Exception as e:  # pragma: no cover - depends on the Hermes build
        warn(f"could not install voice extras automatically ({e}). Run `hermes tools` → Voice, then re-run install.")


def write_config(args):
    cfg = bubble_config.load()
    cfg["hermes_bin"] = hermes_bin()
    for key in ("model", "provider", "browser_profile"):
        val = getattr(args, key, None)
        if val is not None:
            cfg[key] = val
    if getattr(args, "languages", None):
        cfg["languages"] = [x.strip() for x in args.languages.split(",") if x.strip()]
    if cfg.get("model") and not cfg.get("provider"):
        warn("a model without --provider is ignored by the API server; set both, e.g. "
             "--provider anthropic --model claude-haiku-4-5")
    bubble_config.save(cfg)
    ok(f"settings saved to {DATA / 'config.json'}")
    return cfg


def enable_api_server(assume_yes):
    if env_has("API_SERVER_ENABLED") and env_has("API_SERVER_KEY"):
        ok("Hermes API server already configured")
        return False
    if not ask_yes("Enable the local Hermes API server (127.0.0.1 only) for fast bubble answers?", assume_yes):
        warn("skipped — the bubble will use the slower `hermes chat -q` path")
        return False
    env = env_file()
    txt = env.read_text() if env.exists() else ""
    with env.open("a") as f:
        f.write(("\n" if txt and not txt.endswith("\n") else "")
                + "# Local API server used by the Hermes Bubble (127.0.0.1 only)\n"
                + ("" if "API_SERVER_ENABLED=" in txt else "API_SERVER_ENABLED=true\n")
                + ("" if "API_SERVER_KEY=" in txt else f"API_SERVER_KEY={secrets.token_urlsafe(32)}\n"))
    os.chmod(env, 0o600)
    ok("API server enabled in ~/.hermes/.env (restart the gateway to start it: `hermes gateway restart`)")
    return True


def enable_tools():
    for platform_name in ("api_server", "cli"):
        r = run([hermes_bin(), "tools", "enable", "--platform", platform_name, "web", "computer_use"])
        if r.returncode != 0:
            warn(f"could not enable web/computer_use for {platform_name}: {r.stderr.strip()[:200]}")
    ok("web search + screen reading enabled for bubble conversations")
    if not bubble_config.cua_driver():
        warn("Cua Driver not installed yet — app control (YouTube/WhatsApp buttons) needs it. "
             "Run `hermes tools`, enable Computer Use, and grant its permissions.")
    else:
        ok("Cua Driver found")


def codesign_identity():
    out = run(["security", "find-identity", "-v", "-p", "codesigning"]).stdout
    for line in out.splitlines():
        if '"Apple Development' in line or '"Developer ID Application' in line:
            return line.split()[1]
    return "-"


def build_app():
    say("  … building HermesKey.app")
    stop_app()
    macos = APP / "Contents" / "MacOS"
    res = APP / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    arch = "arm64" if platform.machine() == "arm64" else "x86_64"
    r = run(["swiftc", "-O", "-target", f"{arch}-apple-macos14", str(APP_SRC), "-o", str(macos / "HermesKey")])
    if r.returncode != 0:
        raise SystemExit("swift build failed:\n" + r.stderr[-2000:])
    # Icon: reuse the Hermes desktop icon from the local Hermes install when available.
    src_root = Path(run([hermes_bin(), "--print-runtime-command"]).stdout.split("sys.path.insert(0, '")[-1].split("'")[0] or ".")
    for cand in (src_root / "apps/desktop/assets/icon-dark.png", src_root / "assets/icon-dark.png"):
        if cand.exists():
            run(["sips", "-Z", "256", str(cand), "--out", str(res / "hermes.png")])
            break
    icns = src_root / "apps/desktop/assets/icon-dark.icns"
    if icns.exists():
        shutil.copy(icns, res / "AppIcon.icns")
    plist = {
        "CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "HermesKey", "CFBundleExecutable": "HermesKey",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0", "CFBundleIconFile": "AppIcon",
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": "Hermes listens after you hold the Globe key.",
        "NSAppleEventsUsageDescription": "Hermes controls Spotify and BBEdit when you ask it to.",
        "HBDataDir": str(DATA), "HBRuntimeDir": str(RUNTIME),
    }
    import plistlib
    with open(APP / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(plist, f)
    ident = codesign_identity()
    r = run(["codesign", "--force", "--deep", "-s", ident, str(APP)])
    if r.returncode != 0:
        run(["codesign", "--force", "--deep", "-s", "-", str(APP)])
        ident = "-"
    ok(f"HermesKey.app built at {APP} (signed: {'ad-hoc' if ident == '-' else 'developer certificate'})")
    if ident == "-":
        warn("ad-hoc signature: macOS forgets the Input Monitoring grant after each rebuild/update")
    os.chmod(RUNTIME / "launch.sh", 0o755)


def install_login_item():
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{AGENT_LABEL}</string>
<key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-g</string><string>{APP}</string></array>
<key>RunAtLoad</key><true/>
<key>LimitLoadToSessionType</key><string>Aqua</string>
</dict></plist>
""")
    ok("starts automatically at login")


def start_app():
    if app_running():
        return
    run(["open", "-g", str(APP)])
    for _ in range(40):
        if worker_ping():
            break
        time.sleep(0.5)


def stop_app():
    run(["pkill", "-f", f"{APP}/Contents/MacOS/HermesKey"])
    run(["pkill", "-f", f"{RUNTIME}/worker.py"])
    try:
        (DATA / "worker.sock").unlink()
    except OSError:
        pass


def permissions_help():
    say("""
  Last step — permissions (macOS asks once):
    1. Input Monitoring: System Settings → Privacy & Security → Input Monitoring → turn on HermesKey.
       (Opening it now. If HermesKey is missing, click + and add ~/.hermes/bubble/HermesKey.app.)
       Then run:  hermes bubble restart
    2. Microphone: click Allow the first time you hold 🌐.
    3. Notifications / Automation (Spotify, BBEdit): click Allow when asked.
    """)
    run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"])


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_install(args):
    say("Installing Hermes Bubble\n")
    check_platform()
    install_voice_deps()
    write_config(args)
    restart_needed = enable_api_server(args.yes)
    enable_tools()
    build_app()
    install_login_item()
    start_app()
    ok("HermesKey started" if app_running() else "HermesKey built (start it with `hermes bubble start`)")
    if restart_needed:
        say("\n  → Restart the Hermes gateway once so the API server starts:  hermes gateway restart")
    permissions_help()
    say("  Then hold 🌐 for half a second and talk. Full command list: "
        f"{PLUGIN_DIR / 'GUIDE.md'}")


def cmd_uninstall(args):
    stop_app()
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"])
    LAUNCH_AGENT.unlink(missing_ok=True)
    shutil.rmtree(APP, ignore_errors=True)
    ok("HermesKey app and login item removed")
    if args.purge:
        shutil.rmtree(DATA, ignore_errors=True)
        ok(f"settings and logs removed ({DATA})")
    say("  The API server entries in ~/.hermes/.env were left in place (remove them if you don't need them).")
    say("  To remove the plugin itself:  hermes plugins remove hermes-bubble")


def cmd_start(args):
    if not APP.exists():
        raise SystemExit("Not installed yet — run `hermes bubble install`.")
    start_app()
    ok("running" if app_running() else "failed to start — see " + str(DATA / "fnkey.log"))


def cmd_stop(args):
    stop_app()
    ok("stopped")


def cmd_restart(args):
    stop_app()
    time.sleep(1)
    cmd_start(args)


def cmd_status(args):
    cfg = bubble_config.load()
    say("Hermes Bubble status\n")
    (ok if APP.exists() else warn)(f"app: {APP if APP.exists() else 'not built (run hermes bubble install)'}")
    (ok if app_running() else warn)("HermesKey running" if app_running() else "HermesKey not running")
    (ok if worker_ping() else warn)("voice worker responding" if worker_ping() else "voice worker not responding")
    log = DATA / "fnkey.log"
    if log.exists():
        lines = [l for l in log.read_text().splitlines() if "preflight listen access" in l]
        if lines:
            granted = lines[-1].endswith("true")
            (ok if granted else warn)("Input Monitoring granted" if granted else
                                      "Input Monitoring NOT granted — see `hermes bubble install` step 1")
    import urllib.request
    try:
        urllib.request.urlopen(cfg["api_url"].replace("/v1/chat/completions", "/health"), timeout=2)
        ok("Hermes API server reachable")
    except Exception:
        warn("Hermes API server not reachable (bubble falls back to the slower CLI path)")
    (ok if bubble_config.cua_driver() else warn)(
        "Cua Driver found" if bubble_config.cua_driver() else "Cua Driver missing (enable Computer Use in `hermes tools`)")
    say(f"\n  model: {cfg.get('model') or '(Hermes default)'}  provider: {cfg.get('provider') or '(default)'}")
    say(f"  languages: {', '.join(cfg['languages'])}   browser profile: {cfg['browser_profile']}")
    say(f"  logs: {DATA}/ask.log, {DATA}/fnkey.log")


def cmd_config(args):
    cfg = bubble_config.load()
    if not args.set:
        say(json.dumps(cfg, indent=2, ensure_ascii=False))
        return
    for item in args.set:
        if "=" not in item:
            raise SystemExit(f"use key=value (got {item!r})")
        k, v = item.split("=", 1)
        if k not in bubble_config.DEFAULTS:
            raise SystemExit(f"unknown setting {k!r}; known: {', '.join(bubble_config.DEFAULTS)}")
        default = bubble_config.DEFAULTS[k]
        if isinstance(default, list):
            v = [x.strip() for x in v.split(",") if x.strip()]
        elif isinstance(default, bool):
            v = v.lower() in ("1", "true", "yes")
        elif isinstance(default, int):
            v = int(v)
        elif isinstance(default, float):
            v = float(v)
        cfg[k] = v
        ok(f"{k} = {v!r}")
    bubble_config.save(cfg)
    say("  Restart to apply:  hermes bubble restart")


def setup_parser(p: argparse.ArgumentParser):
    sub = p.add_subparsers(dest="bubble_cmd")
    i = sub.add_parser("install", help="one-shot setup")
    i.add_argument("--model", help="model for bubble conversations (default: your Hermes default)")
    i.add_argument("--provider", help="provider for --model, e.g. anthropic, openrouter")
    i.add_argument("--languages", help="comma-separated languages you speak, e.g. en,fr,ar")
    i.add_argument("--browser-profile", dest="browser_profile", help='Chrome profile dir (default "Default")')
    i.add_argument("--yes", "-y", action="store_true", help="answer yes to every prompt")
    u = sub.add_parser("uninstall", help="remove the app and login item")
    u.add_argument("--purge", action="store_true", help="also delete settings and logs")
    sub.add_parser("start", help="start the bubble")
    sub.add_parser("stop", help="stop the bubble")
    sub.add_parser("restart", help="restart the bubble (after permission or config changes)")
    sub.add_parser("status", help="health check")
    c = sub.add_parser("config", help="show or change settings")
    c.add_argument("set", nargs="*", help="key=value pairs, e.g. languages=en,fr model=claude-haiku-4-5")


def main(args):
    handlers = {"install": cmd_install, "uninstall": cmd_uninstall, "start": cmd_start, "stop": cmd_stop,
                "restart": cmd_restart, "status": cmd_status, "config": cmd_config}
    fn = handlers.get(getattr(args, "bubble_cmd", None))
    if not fn:
        say(DESCRIPTION)
        return 0
    fn(args)
    return 0
