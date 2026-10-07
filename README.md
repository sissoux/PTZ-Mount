# PTZ mount: software

Pan / tilt / zoom head for a DSLR, driven by a **BigTreeTech SKR Pico**
(RP2040 + 4x TMC2209) connected to a **Raspberry Pi 4** over UART.

* **RP2040 firmware (C)**: real-time motion. Step generation, acceleration
  ramps, homing stop, soft limits, watchdogs.
* **Raspberry Pi daemon (Python)**: everything else. Klipper-style config
  file, motion logic, presets, web UI, and network control (WebSocket,
  UDP, VISCA over IP).

Joystick-to-motor latency is a few milliseconds, because the MCU runs in
velocity mode instead of executing a pre-planned move queue like Klipper. See
[docs/architecture.md](docs/architecture.md).

## Repository layout

```
config/ptz.cfg          <- THE file to edit: pins, gearing, limits, speeds, currents
docs/
  architecture.md       design, latency budget, safety layers, roadmap
  protocol.md           binary host <-> MCU protocol
  raspberry-pi-setup.md UART setup, firmware flashing, service install
firmware/               RP2040 firmware (C, Pico SDK)
host/
  ptz/
    config.py           ptz.cfg parser (Klipper-like syntax)
    protocol.py         binary protocol (COBS + CRC16)
    mcu.py              serial link, request/ACK
    sim.py              simulated MCU (run everything without hardware)
    motion.py           MotionController: jog, goto, homing, presets
    tmc2209.py          driver register computation (current, microsteps...)
    presets.py          preset storage (JSON)
    api/                HTTP + WebSocket, UDP, VISCA over IP, shared commands
    web/                web UI (plain HTML/JS, no build step)
  tests/                pytest suite (runs against the simulator)
clients/                example network clients (UDP, USB gamepad bridge)
deploy/                 systemd service + install script for the Pi
```

## Quick start on a PC (no hardware)

```sh
cd host
python -m venv .venv
.venv/Scripts/activate            # Windows   (Linux: source .venv/bin/activate)
pip install -e .[dev]
python -m ptz -c ../config/ptz.cfg --sim
```

Open http://localhost:8080/, press **Home all**, then drive with the pad,
the arrow keys or a gamepad.

Run the tests:

```sh
pytest
```

## On the Raspberry Pi

Follow [docs/raspberry-pi-setup.md](docs/raspberry-pi-setup.md): enable the
UART, stop Klipper, build and flash the firmware, then run `./deploy/install.sh`.

## Control interfaces

| Interface | Port | Use |
|---|---|---|
| Web UI | HTTP 8080 | Pad, zoom rocker, keyboard, browser gamepad, presets |
| WebSocket `/ws` | 8080 | JSON commands and status push (custom apps) |
| REST `/api/cmd`, `/api/status` | 8080 | Scripting (`curl`) |
| JSON over UDP | 9000 | Lowest latency, network joysticks |
| VISCA over IP | UDP 52381 | PTZ keyboards, Bitfocus Companion, OBS, vMix |

## Web UI features

* **Live control**: pad, zoom rocker, keyboard, gamepad, presets, and a
  large position readout per axis (unit label set by `units:` in each axis).
* **Homing first**: an axis that is not homed cannot move at all, whether
  by jog, move, preset or replay, from any source. Homing itself is always
  allowed. Releasing the motors or a board reboot clears the homing. Set
  `require_homing: False` in `[motion]` to allow jogging unhomed axes.
* **Speed and Acceleration in real units** (°/s, °/s²) for jogging, moves,
  presets and the move to the start of a replay. One value for all axes,
  capped by each axis maximum, or separate values per axis with
  **Advanced**. **Ease in/out** is shown in seconds of ramp. Stored on the Pi.
* **Record & replay**: press Record, move the head by any means (web,
  gamepad, UDP joystick, VISCA, presets), press Stop.
  * *Start when moving* (default): the clock starts with the first
    movement and the motionless end is trimmed.
  * *Continuous path* records the real path. *Keypoints* records only the
    points you add with "+ Keypoint", with their timing, and replays a
    smooth curve through them.
  * Replay once or in a loop, at 0.1x to 4x, adjustable while playing.
  * Download a recording as JSON, or upload one. Uploads are validated
    against the axes and soft limits and never overwrite an existing name.
* **Connected devices**: the 👥 counter lists the web pages and the network
  controllers (UDP, VISCA) seen in the last 10 s.
* **Blocking mode** (🔒 Take control): only your page can move the head. The
  others, plus UDP, VISCA and REST, can only Stop and E-stop. The lock is
  released when you click again or close the page. From the Pi, a stuck lock
  can be forced open:
  `curl -X POST localhost:8080/api/cmd -d '{"cmd":"unlock","force":true}'`
* **Debug mode** (top right): verbose logging and a live console showing
  every command with its source, rejections, MCU events and endstop changes,
  plus a driver diagnostics button.

All of them share the same command set, documented in
[host/ptz/api/commands.py](host/ptz/api/commands.py). Example:

```json
{"cmd": "jog", "pan": 0.5, "tilt": -0.2, "zoom": 0}
{"cmd": "goto", "pan": 30, "tilt": 10, "speed": 0.5}
{"cmd": "preset_recall", "preset": 2}
```

Jog values are normalized from -1 to 1 and must be repeated at least every
`jog_timeout` (0.5 s by default) while the stick is held. Otherwise the head
stops by itself.

## Status

* Host: implemented and covered by tests against the simulator (protocol,
  config, TMC registers, homing, synchronized goto, soft limits, jog timeout,
  e-stop, presets, VISCA).
* Firmware: complete first version. It has been syntax-checked, but **not yet
  compiled with the Pico SDK nor run on the board**.
* `config/ptz.cfg` carries over the pins, polarities, gearing, travel limits,
  endstop positions and driver currents of the original Klipper setup
  (`config/PreviousConfig.cfg`). A test checks that they stay in sync.

## Useful axis options

| Option | Effect |
|---|---|
| `enabled: False` | Axis ignored entirely (hardware not fitted) |
| `home_with_all: False` | Axis skipped by "Home all" and default homing, still homable alone |
| `endstop_guard: auto` | MCU stops the axis on its switch outside homing, only if the switch is at the end of travel |
