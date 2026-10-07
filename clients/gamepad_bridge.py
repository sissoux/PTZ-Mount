#!/usr/bin/env python3
"""USB gamepad -> PTZ head over the network (JSON/UDP).

Run it on any computer (or a small Pi Zero inside a hardware joystick box)
with a gamepad plugged in:

    pip install pygame
    python gamepad_bridge.py 192.168.1.50

The default mapping suits an Xbox-style controller. Edit the constants below
for your device (run with --debug to see raw axis and button numbers).
"""
import argparse
import json
import socket
import time

import pygame

# ---- mapping (edit me) -------------------------------------------------------
AXIS_PAN, AXIS_TILT, AXIS_ZOOM = 0, 1, 3           # left stick X/Y, right stick Y
INVERT = {"pan": False, "tilt": True, "zoom": True}
BTN_STOP = 1                                        # B
BTN_HOME = 7                                        # Start
BTN_SLOW = 4                                        # LB held = precision mode (30 %)
PRESET_BUTTONS = {0: 1, 2: 2, 3: 3}                 # A/X/Y -> recall presets 1..3
RATE_HZ = 50
# ------------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        raise SystemExit("no gamepad found")
    js = pygame.joystick.Joystick(0)
    js.init()
    print(f"using '{js.get_name()}' -> {args.host}:{args.port}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.host, args.port)

    def send(**msg):
        sock.sendto(json.dumps(msg).encode(), dest)

    idle_sent = False
    while True:
        for ev in pygame.event.get():
            if ev.type == pygame.JOYBUTTONDOWN:
                if args.debug:
                    print("button", ev.button)
                if ev.button == BTN_STOP:
                    send(cmd="stop")
                elif ev.button == BTN_HOME:
                    send(cmd="home")
                elif ev.button in PRESET_BUTTONS:
                    send(cmd="preset_recall", preset=PRESET_BUTTONS[ev.button])

        def axis(i, name):
            v = js.get_axis(i) if i < js.get_numaxes() else 0.0
            return -v if INVERT[name] else v

        scale = 0.3 if js.get_numbuttons() > BTN_SLOW and js.get_button(BTN_SLOW) else 1.0
        values = {n: round(axis(i, n) * scale, 3) for n, i in
                  (("pan", AXIS_PAN), ("tilt", AXIS_TILT), ("zoom", AXIS_ZOOM))}
        if args.debug:
            print([round(js.get_axis(i), 2) for i in range(js.get_numaxes())], end="\r")

        # deadband/expo are applied on the head; just avoid flooding when idle
        active = any(abs(v) > 0.02 for v in values.values())
        if active or not idle_sent:
            send(cmd="jog", **values)
            idle_sent = not active
        time.sleep(1.0 / RATE_HZ)


if __name__ == "__main__":
    main()
