#!/usr/bin/env bash
# Pull the latest code and restart the daemon.
#   ./deploy/update.sh            pull from GitHub, then restart
#   ./deploy/update.sh --no-pull  just restart (after editing on the Pi)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ "${1:-}" != "--no-pull" ]; then
    before="$(git rev-parse HEAD)"
    git pull --ff-only
    # reinstall only when dependencies changed
    if ! git diff --quiet "$before" HEAD -- host/pyproject.toml; then
        "$ROOT/.venv/bin/pip" install -e "$ROOT/host"
    fi
fi

sudo -n systemctl restart ptz 2>/dev/null || sudo systemctl restart ptz
sleep 2
systemctl --no-pager --lines=0 status ptz | head -3
journalctl -u ptz --no-pager -n 8 -o cat | grep -v aiohttp.access || true
