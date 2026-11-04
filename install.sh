#!/usr/bin/env bash
# Installs or updates Encore on Debian or Ubuntu. Run as root. Safe to run again.
# Optional settings: ENCORE_DIR, ENCORE_PORT, ENCORE_USER, ENCORE_REPO, ENCORE_BRANCH.
set -euo pipefail

REPO="${ENCORE_REPO:-https://github.com/marctew/Encore.git}"
BRANCH="${ENCORE_BRANCH:-main}"
DIR="${ENCORE_DIR:-/opt/encore}"
PORT="${ENCORE_PORT:-3346}"
RUN_AS="${ENCORE_USER:-encore}"

fail() { echo "Encore install stopped: $1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run this as root."
command -v apt-get >/dev/null || fail "this installer needs Debian or Ubuntu."

echo "Installing what Encore needs..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates git python3 python3-venv flac ffmpeg >/dev/null

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "Encore needs Python 3.10 or newer."

if ! id "$RUN_AS" >/dev/null 2>&1; then
    useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin "$RUN_AS"
fi

echo "Fetching Encore into $DIR..."
if [ -d "$DIR/.git" ]; then
    git -c "safe.directory=$DIR" -C "$DIR" pull --ff-only origin "$BRANCH"
else
    [ ! -e "$DIR" ] || [ -z "$(ls -A "$DIR")" ] || fail "$DIR already has files in it. Empty it or set ENCORE_DIR."
    git clone --quiet --branch "$BRANCH" "$REPO" "$DIR"
fi

echo "Setting up Python packages..."
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --quiet --upgrade pip
"$DIR/venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

DATA=/var/lib/encore
if [ -d "$DIR/data" ] && [ ! -e "$DATA" ]; then
    echo "Moving Encore's data from $DIR/data to $DATA..."
    systemctl stop encore 2>/dev/null || true
    mv "$DIR/data" "$DATA"
fi
mkdir -p "$DATA/photos"
chown -R "$RUN_AS": "$DIR" "$DATA"
chmod 700 "$DATA"

url="http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"

if [ -d /run/systemd/system ]; then
    sed -e "s|@DIR@|$DIR|g" -e "s|@USER@|$RUN_AS|g" -e "s|@PORT@|$PORT|g" "$DIR/deploy/encore.service" \
        | grep -v 'install.sh writes\|filled in by install.sh' > /etc/systemd/system/encore.service
    systemctl daemon-reload
    systemctl enable --quiet encore
    systemctl restart encore
    echo
    echo "Encore is running at $url"
    echo "Open Settings there to add your folders, indexers, SABnzbd and Plex."
else
    echo
    echo "Encore is installed, but this system has no systemd, so nothing was started."
    echo "Start it with: cd $DIR && ENCORE_PORT=$PORT $DIR/venv/bin/python -m encore"
fi
