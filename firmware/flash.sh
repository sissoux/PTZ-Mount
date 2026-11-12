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

# The SDK's own picotool (build/_deps/picotool-build) is built without USB
# support (it only makes the .uf2): build a standalone one with USB once,
# from the same source (needs libusb-1.0-0-dev).
PICOTOOL="$(command -v picotool || true)"
if [ -z "$PICOTOOL" ]; then
    PICOTOOL="$HOME/.picotool-usb/picotool"
    if [ ! -x "$PICOTOOL" ]; then
        echo "==> building picotool with USB support in ~/.picotool-usb"
        cmake -S build/_deps/picotool-src -B "$HOME/.picotool-usb" \
            -DPICO_SDK_PATH="$PWD/build/_deps/pico_sdk-src" -DCMAKE_BUILD_TYPE=Release
        cmake --build "$HOME/.picotool-usb" -j4
    fi
fi
# -f: force a running board into BOOTSEL, -x: run the new firmware afterwards
"$PICOTOOL" load -f -x build/ptz_firmware.uf2
echo "Flashed. The daemon reconfigures the board by itself (BOOT event)."
