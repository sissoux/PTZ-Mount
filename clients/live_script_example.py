#!/usr/bin/env python3
"""Example "main script" for a live event, driving the mount over WebSocket.

    pip install aiohttp
    python live_script_example.py --host PTZMount.local

What it shows (adapt to your run sheet):
  1. connect and take exclusive control (blocking mode)
  2. home the axes and wait until done
  3. go to a preset, then to absolute positions, waiting for arrival
  4. a short continuous jog
  5. race tracking: arm a learned track, fire GO, watch progress
  6. clean exit (stop, release the lock)
"""
import argparse
import asyncio
import logging

from ptz_client import JogStream, PTZClient, PTZError


async def main(args) -> None:
    async with PTZClient(args.host, args.port, token="mainscript01") as ptz:
        print("connected; axes:", list(ptz.status["axes"]))
        ptz.on_status = None            # or a callback(status) called ~20 times per second

        await ptz.lock()                # web pages / joysticks can only Stop or E-stop now
        try:
            if ptz.status["estop"]:
                await ptz.clear_estop()

            if not ptz.is_homed("pan", "tilt"):
                print("homing...")
                await ptz.home(["pan", "tilt"], wait=True)      # ends parked at 0 / 0
            print("positions:", ptz.positions())

            await ptz.set_motion(speed=30, accel=50, smoothing=0.3)   # deg/s, deg/s2

            if "1" in ptz.presets:
                await ptz.preset(1, wait=True)
                print("at preset 1:", ptz.positions())

            await ptz.goto(pan=25, tilt=-5, wait=True)
            await ptz.goto(pan=-25, speed=0.5, wait=True)               # half speed
            print("after moves:", ptz.positions())

            async with JogStream(ptz) as jog:                          # continuous jog
                jog.set(pan=0.4)
                await asyncio.sleep(1.5)
                jog.set(pan=0, tilt=0.3)
                await asyncio.sleep(1.0)
            print("after jog:", ptz.positions())

            if args.track:
                await ptz.track_set(auto_rearm=True, auto_adjust=True)
                await ptz.track_arm(args.track, wait=True)              # at the start point
                print(f"armed on {args.track}; firing GO")
                await ptz.track_go()                                    # normally: timing loop
                await ptz.wait_lap(timeout=600)
                t = ptz.status["tracking"]
                print(f"lap done: {t['runs']} lap(s), phase {t['phase']}")
                await ptz.track_abort()

            await ptz.goto(pan=0, tilt=0, wait=True)
        except PTZError as e:
            print("refused by the mount:", e)
            await ptz.stop()
        finally:
            await ptz.unlock()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="PTZMount.local")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--track", help="name of a learned track to arm and fire")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING)
    asyncio.run(main(a))
