#!/usr/bin/env bash
# Build the firmware and flash it over USB, without pressing BOOT.
#
# Needs: the SKR Pico USB-C port connected to the Pi, a PTZ firmware already
# running on it (its USB interface accepts a "reboot to bootloader" request),
# and the picotool udev rule (deploy/setup-remote.sh).
# First flash, or if the board is not answering: hold BOOT, press RESET,
# release BOOT, then run this script again.
set -euo pipefail

cd "$(dirname "$0")"
if [ ! -d build ]; then
    cmake -B build -DPICO_SDK_FETCH_FROM_GIT=ON
fi
cmake --build build -j4

PICOTOOL="$(command -v picotool || echo build/_deps/picotool-build/picotool)"
# -f: force a running board into BOOTSEL, -x: run the new firmware afterwards
"$PICOTOOL" load -f -x build/ptz_firmware.uf2
echo "Flashed. The daemon reconfigures the board by itself (BOOT event)."
