#!/usr/bin/env bash
# One-time setup for working on the Pi remotely (VS Code Remote-SSH, scripts).
# Run once, as the normal user, from the repository root: ./deploy/setup-remote.sh
#
#  1. sudoers rule: start/stop/restart the ptz service without a password
#     (only these exact commands, nothing else)
#  2. udev rule: picotool may talk to the RP2040 over USB without sudo, so
#     firmware/flash.sh can flash the board with no BOOT button press
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"

echo "==> sudoers rule for the ptz service (/etc/sudoers.d/ptz)"
TMP="$(mktemp)"
{
    for cmd in start stop restart; do
        echo "$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl $cmd ptz, /bin/systemctl $cmd ptz"
    done
} > "$TMP"
sudo visudo -cf "$TMP"            # refuse to install a broken file
sudo install -m 0440 -o root -g root "$TMP" /etc/sudoers.d/ptz
rm -f "$TMP"

echo "==> udev rule for picotool (USB access without sudo)"
RULE="$(ls "$ROOT"/firmware/build/_deps/picotool-src/udev/*.rules 2>/dev/null | head -1 || true)"
if [ -n "$RULE" ]; then
    sudo install -m 0644 "$RULE" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "    installed $(basename "$RULE")"
else
    echo "    picotool not built yet: build the firmware first, then rerun this script"
fi
if ! id -nG "$USER_NAME" | grep -qw plugdev; then
    sudo usermod -aG plugdev "$USER_NAME"
    echo "    added $USER_NAME to plugdev (log out and back in)"
fi

echo
echo "Done. Test with: sudo -n systemctl restart ptz && echo OK"
