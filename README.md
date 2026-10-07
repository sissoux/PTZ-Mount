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

## Race tracking

1. **Learn.** In "Race tracking", enter a name and press **Start learning**.
   Follow the cars by hand (pad, gamepad, joystick) for as many laps as you
   like. Press **Space** (or LAP) each time a car crosses the start / timing
   line. Press **Stop & build**.
2. **Average.** The session is cut into laps at your marks. Each lap is
   stretched to the same length and the laps are averaged point by point.
   Laps far from the others are rejected automatically. The table shows each
   lap's time and deviation; untick laps and press Rebuild to choose by hand.
   The track replays at the mean lap time. The raw session is kept as
   "name (raw)".
3. **Track.** Select the track and press **ARM**: the head goes to the start
   point and waits. On the timing signal press **GO** (or Enter), or let the
   timing system send it:
   ```sh
   echo '{"cmd":"track_go"}' | nc -u -w0 <pi-address> 9000      # UDP
   curl -X POST <pi-address>:8080/api/cmd -d '{"cmd":"track_go"}'
   ```
   `track_go` is accepted even in blocking mode: it only fires the lap the
   operator armed. A *Target lap* time (s) rescales the replay speed for
   faster or slower cars. With *Auto re-arm*, the head returns to the start
   point after each lap. A joystick move, Stop or Abort cancels.
4. **GO during a lap** (the car was faster than the replay, or a GO came while
   the head was still returning to the start) drops the current lap and
   starts the next one immediately. The head glides from its current
   position and speed onto the new lap, within half of each axis's speed and
   acceleration limits. It does not jump back to the start point.
5. **Auto adjust lap time**: GO-to-GO intervals are measured. If one is
   within the tolerance (±30 % by default) of the lap time in use, the next
   lap uses the halfway value (17 s in use, 18 s measured: 17.5 s next).
   Intervals outside the tolerance are ignored, for example a crash, a slow
   lap, or a GO triggered by another car.

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
* **Race tracking**: learn a lap path, then fire it when a car crosses the
  timing line. See below.
* **⚙ Config** page: edit the configuration in the browser. The file is
  checked before saving, the previous version is kept as a backup, and
  "Save & restart" applies it. Edits go to `~/.ptz/ptz.cfg`, which replaces
  `config/ptz.cfg` without modifying the repository, so `git pull` keeps
  working. "Reset to default" goes back to the repository file. If the edited
  file ever fails to load, the daemon starts with the default and says so.
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
