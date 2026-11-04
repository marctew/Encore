#!/usr/bin/env bash
# Deploy Encore from this checkout: install dependencies if they changed, check the code compiles,
# restart, wait for /healthz. Run as root (or with sudo). A download in progress carries on after
# a restart; checking and filing don't, so the script waits for those.
#
#   scripts/update.sh            restart now; refuses while an album is being checked or filed
#   scripts/update.sh --pull     git pull first
#   scripts/update.sh --force    restart even mid-filing (the job goes back in the queue)
#   scripts/update.sh --if-idle  restart only if nothing is being checked or filed; otherwise leave the
#                                new code for the next restart and still succeed (the design updater uses this)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

SERVICE=encore
PORT="$(systemctl show "$SERVICE" -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^ENCORE_PORT=//p')"
HEALTH="http://127.0.0.1:${PORT:-3346}/healthz"
PULL=0 FORCE=0 IDLE=0
for a in "$@"; do
    case "$a" in
        --pull) PULL=1 ;;
        --force) FORCE=1 ;;
        --if-idle) IDLE=1 ;;
        *) echo "Unknown option: $a" >&2; exit 2 ;;
    esac
done
owner=$(stat -c %U .)
as_owner() { if [ "$(id -un)" = "$owner" ]; then "$@"; else sudo -u "$owner" -- "$@"; fi; }
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

# /healthz's busy list ("idle" when empty), or nothing if it doesn't answer. Python, not curl.
health() {
    "$REPO/venv/bin/python" - "$HEALTH" "$1" <<'EOF' 2>/dev/null || true
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=float(sys.argv[2])) as r:
    print(", ".join(json.load(r).get("busy", [])) or "idle")
EOF
}

if [ "$PULL" = 1 ]; then
    as_owner git pull --ff-only -q
    # Carry on with the script as pulled, not the copy bash already has open.
    args=()
    for a in "$@"; do [ "$a" = --pull ] || args+=("$a"); done
    exec bash "$REPO/scripts/update.sh" "${args[@]}"
fi

# Dependencies, only when requirements.txt has changed since the last install.
STAMP=venv/.requirements.sha256
WANT="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]; then
    as_owner venv/bin/pip install -q -r requirements.txt
    echo "$WANT" | as_owner tee "$STAMP" >/dev/null
fi

as_owner venv/bin/python -m compileall -q encore wsgi.py >/dev/null \
    || { echo "The code doesn't compile; not restarting." >&2; exit 1; }

busy="$(health 5)"
[ "$busy" = idle ] && busy=""
if [ -n "$busy" ] && [ "$FORCE" = 0 ]; then
    if [ "$IDLE" = 1 ]; then
        echo "Busy ($busy), so not restarting. The new code loads on the next restart."
        exit 0
    fi
    echo "Busy ($busy). Try again when it's finished, or use --force." >&2
    exit 1
fi

as_root systemctl restart "$SERVICE"
for _ in $(seq 60); do
    if [ -n "$(health 2)" ]; then
        echo "$SERVICE restarted at $(as_owner git rev-parse --short HEAD) and healthy."
        exit 0
    fi
    sleep 1
done
echo "$SERVICE didn't answer $HEALTH within a minute:" >&2
as_root journalctl -u "$SERVICE" -n 30 --no-pager >&2
exit 1
