#!/bin/bash
# Run a Hermes Bubble runtime script (ask | worker) inside Hermes' own Python runtime,
# so it can use Hermes' voice stack (faster-whisper, TTS, web search) directly.
RUNTIME="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="${1:-worker}"
DATA="${HERMES_BUBBLE_HOME:-${HERMES_HOME:-$HOME/.hermes}/bubble}"
mkdir -p "$DATA"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"

HERMES="$(/usr/bin/python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("hermes_bin",""))
except Exception: print("")' "$DATA/config.json")"
[ -x "$HERMES" ] || HERMES="$(command -v hermes)"
if [ -z "$HERMES" ]; then echo "hermes not found" >> "$DATA/$SCRIPT.out"; exit 1; fi

# `hermes --print-runtime-command` → [python, -I, -c, "...sys.path.insert(0, '<src>')..."]
read -r PY SRC < <("$HERMES" --print-runtime-command 2>/dev/null | /usr/bin/python3 -c '
import json, re, sys
cmd = json.load(sys.stdin)
m = re.search(r"sys\.path\.insert\(0, \x27([^\x27]+)\x27\)", " ".join(cmd))
print(cmd[0], m.group(1) if m else "")')

exec "$PY" -I -c "
import os, sys, runpy
for k in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'): os.environ.pop(k, None)
sys.path.insert(0, '$SRC')
sys._hermes_pin_default_home = True
import hermes_bootstrap
os.environ.setdefault('HERMES_HOME', str(__import__('hermes_constants').get_default_hermes_root()))
sys.path.insert(0, '$RUNTIME')
runpy.run_path('$RUNTIME/$SCRIPT.py', run_name='__main__')
" >> "$DATA/$SCRIPT.out" 2>&1
