#!/usr/bin/env bash
# Install the PTZ daemon on a Raspberry Pi as a systemd service.
# Usage: ./deploy/install.sh   (from the repository root, as the normal user)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"

echo "==> Python virtual environment in $ROOT/.venv"
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install --upgrade pip
"$ROOT/.venv/bin/pip" install -e "$ROOT/host"

echo "==> systemd service"
sed -e "s|@ROOT@|$ROOT|g" -e "s|@USER@|$USER_NAME|g" "$ROOT/deploy/ptz.service" \
    | sudo tee /etc/systemd/system/ptz.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable ptz

if systemctl is-active --quiet klipper 2>/dev/null; then
    echo
    echo "WARNING: klipper.service is running and holds the serial port."
    echo "         Stop it with: sudo systemctl disable --now klipper moonraker"
fi
if ! id -nG "$USER_NAME" | grep -qw dialout; then
    echo "WARNING: $USER_NAME is not in the 'dialout' group: sudo usermod -aG dialout $USER_NAME"
fi

sudo systemctl restart ptz
echo
echo "Done. Web UI: http://$(hostname -I | awk '{print $1}'):8080/"
echo "Logs: journalctl -u ptz -f"
