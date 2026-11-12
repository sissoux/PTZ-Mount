# PTZ mount: software

Pan / tilt / zoom head for a DSLR, driven by a **BigTreeTech SKR Pico**
(RP2040 + 4x TMC2209) connected to a **Raspberry Pi 4** over UART. Pan and
tilt are steppers; the zoom ring is turned by a hobby servo on the SKR
Pico's SERVOS header (gpio29), see *Zoom servo* below.

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
  api.md                control API for scripts (WebSocket, UDP, REST, VISCA)
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
6. **Several cars.** Click a car under *Car followed* (or type its race
   number), or let the timing software do it: `{"cmd":"await_car","car":5}`.
   The list (12 cars at most) fills as cars are seen, or the software sends
   it: `{"cmd":"cars_set","cars":[5,12,44]}`; *Flush car list* or
   `cars_flush` empties it. The head goes
   to the start line and waits for that car's next crossing, which the
   software sends as `{"cmd":"lap","car":5}` (UDP or HTTP, like
   `track_go`). While **learning**, the lap in progress is dropped and the
   next crossing starts a new lap, so you can switch cars at any time and
   only follow by hand; the crossings replace Space. While **tracking**, the
   head re-arms and only that car's crossing fires the lap. The software may
   send the crossings of every car: only the car followed drives the head,
   and each car's last / best lap time is shown (*Names & lap times*, where
   cars can be named). Auto adjust measures each car on its own crossings,
   and switching to a car uses its own lap time.
7. **🏁 Follow first** follows the leading car, whatever its number. The
   leader is the car with the most crossings since *Reset lap times* (press
   it at the start of the race), or the position the software sends with
   each lap (`"position": 1`). When another car takes the lead it crosses
   the line first, so the head switches to it right there: the crossing is
   its GO (or its lap mark while learning). Clicking a car turns it off.

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
* **Controller panel**: any gamepad or custom HID joystick seen by Windows.
  The device you touch is used automatically, even if others are connected.
  Axes and buttons are shown live and can be mapped, including a "detect"
  helper for axes. The mapping is saved per device in the browser.
  * Default for a custom controller: X/Y = pan/tilt, Z slider = joystick
    speed, buttons 1-5 = presets 1-5, button 6 = Lap / GO.
  * Lap / GO marks a lap while learning, and fires GO while a track is armed
    or running.
  * The slider can drive the joystick speed (5-100 %), the zoom position
    (slider = zoom from wide to tele of the lens fitted, follows until
    reached), or the zoom rate (centre = stop). Choose it under *Controller
    slider*, below the lens (shown when the controller has a slider, also
    in the Live view); the choice is kept per controller in the browser.
  * Keep the page visible: browsers stop reading controllers in background
    tabs.
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
* **Path editor** (✎ on a recording, or ✎ Edit next to ARM for a track):
  fine-tune a path on site. The recording is reduced to a few control points
  and the curve shown is exactly the one the head replays.
  * *Aim map* (pan → / tilt ↑) with the soft limits, presets, the original
    recording and, for a track, the learned laps in grey. *Timelines* for
    pan, tilt and zoom, plus a speed lane that turns red where an axis would
    exceed its maximum speed.
  * Drag points on the map or the timelines, double-click a timeline to add
    a point, Delete removes one.
  * *Time lane*: one diamond per keyframe. Drag it left / right to change
    when the head gets there (all axes of that point move together, between
    its neighbours); Shift+drag moves it and every later keyframe. The
    numbers are the time between keyframes. ±0.1 / ±0.5 s buttons in the
    point panel.
  * The on-track workflow: select a point (the head goes there when *Live* is
    on), correct the aim with the joystick, press **Set from head**. Nudge
    buttons (±0.1 / ±1) for small corrections.
  * Whole path: offset per axis, new duration (lap time), trim start / end
    at the playhead, rebuild points with another simplification tolerance,
    add zoom to a path recorded without it.
  * Undo / redo (Ctrl+Z / Ctrl+Y), *Preview on head* at ×0.25 to ×1 without
    saving, *Save* (the previous version goes to
    `~/.ptz/recordings/backups/`) or *Save as*. Re-ARM a track to use the
    saved version.
* **Soft limits** (middle column): jog the head to a boundary and press Set
  for left / right pan and bottom / top tilt (and zoom min / max if needed).
  With *Enabled* ticked, nothing can drive the head past them: jog, presets,
  replays, tracking and scripts all stop at the limits. Untick it to use the
  whole travel again; the learned values are kept on the Pi.
* **Lens** (under the pad): choose the lens fitted. Zoom up / T, PgUp and
  VISCA tele then always zoom in, whichever way the lens's ring turns
  (Sony and Canon turn opposite ways), the zoom stops at the ring's two
  ends, and the zoom shows as a focal length (readout, axes table; *Go to*
  takes mm). *+ Learn a new lens…* asks for its name and min / max focal.
  With the zoom servo, move the zoom with the slider, the steps or the
  zoom rocker (only the zoom moves) and mark: *Wide end here* at the min
  focal, each focal printed on the barrel (18-135: 35, 50, 85) with *Mark
  here*, *Tele end here* at the max focal, then *Save the lens*. (A stepper
  zoom with an endstop is homed first, stepped to the other end of the ring
  and you say whether it is zoomed in or out.) The focal length is
  interpolated between the marks. With a lens the **Zoom speed** slider is
  in focal mm/s: the servo turns faster or slower along the ring so the
  focal length changes at that rate even where the ring is not linear.
  ✎ edits the lens (*Keep the ends learned before* to only change the name,
  focals or marks).
  *Max tilt with this lens*: *Adjust* frees the tilt (soft limits off, up
  to the configured travel), jog to the lowest safe tilt, *Record max tilt
  here*. While that lens is selected it replaces the soft limit's tilt max.
* **Base level** (middle column), for a mount standing at an angle: aim the
  camera level (its electronic level at 0, or the horizon centred) and press
  *Mark level here*, at two pan angles at least 30° apart, then tick *Level
  jog*. Pan jogs then keep the same height above the horizon: the tilt
  follows on its own. The two values can also be typed: the tilt that looks
  level at pan 0 and at pan +90. Only jogging is corrected (presets and
  replays are exact mount positions); the picture's own roll cannot be
  removed by a pan / tilt head.
* **▣ Live** (top right, or open `/live`, e.g. `http://<pi-address>:8080/live`): a
  simpler page for the event with the jog pad, position, Home / Stop,
  presets and race tracking only. ▣ Full goes back; the choice is
  remembered by the browser.
* **⏻ Shutdown** (top right): the head goes to its rest position (pan 0°,
  tilt at its max end, +32° here; `shutdown_position` per axis in the configuration), the
  motors are turned off, every file is flushed to the SD card and the Pi
  powers off. The page tells when it is safe to unplug (about 15 s after the
  connection drops). E-STOP stays available during the rest move. Powering
  off needs the sudoers rule of `deploy/setup-remote.sh`; without it the
  page ends on "Safe to unplug" with the Pi still on but idle.
  State files (settings, presets, recordings, edited configuration) are
  always written atomically and flushed, so an unexpected power cut never
  leaves a half-written file.
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
* **🔧 Axis debug** page (`/debug`), for checking a motor's wiring with the
  mount dismantled: choose the axis, a number of microsteps (quick buttons:
  1 full step, ¼ turn, 1 turn...) and a speed in steps/s, then *− steps* /
  *+ steps*. No homing and no limits (the axis loses its homing); Stop or
  Esc ends the move. It shows the step counter (with a zero), the endstop,
  *Axis on* / *Driver powered* switches, and a TMC2209 check: UART answering,
  open coil A / B, short to ground, temperature.
* **Debug mode** (top right): verbose logging and a live console showing
  every command with its source, rejections, MCU events and endstop changes,
  plus a driver diagnostics button.

**Driving the mount from a program** (for example a live-event script): see
[docs/api.md](docs/api.md) for the complete API reference (WebSocket, UDP,
REST, VISCA), the Python client
[clients/ptz_client.py](clients/ptz_client.py) and the example script
[clients/live_script_example.py](clients/live_script_example.py).

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

## Zoom servo

The zoom is a hobby servo (it holds its own position: no endstop, no
homing). Plug it into the SKR Pico **SERVOS** header (the BLTouch control
pin): signal = **gpio29**, 5V, GND. A zoom servo can draw 1-2 A when it
starts or stalls: power its + wire from a separate 5-6 V BEC (within the
servo's rating), grounds joined, only the signal from the SKR Pico.

`[axis zoom]` in `ptz.cfg` has `servo_pin: gpio29` and the pulse widths
of its two ends (`minimum_pulse_width: 500`, `maximum_pulse_width: 2500`
us for this 270° servo, or seconds like Klipper's `[servo]`); the position
is 0..100 % of that range. Changing the range later keeps the saved
positions (lens ends and marks, presets, last position) at the same pulse
widths. The servo starts with the zoom "on" checkbox, at its last position
(remembered on the Pi).

**The servo never starts by itself.** With no pulse a servo is released
and its ring may be turned by hand; its first pulse would send it at full
speed to the old position. So when the zoom is switched on (and after a
restart of the Pi or the board, *Motors off*, zoom off) the web UI asks
where the zoom is: the lens fitted and its focal now (buttons for the
focals printed on the barrel; if unsure, turn the ring by hand to one).
The servo starts there, then only corrects a small error. *Start at the
last position* is for a ring nobody touched. Until then the zoom gets no
pulse and does not move (*▶ Start the zoom servo…* in the Lens row).

**No lens selected = zoom locked.** A servo forces its way to the position
asked: past a lens's ring end it could break the lens. So with no lens
selected nothing moves the zoom (rocker, gamepad, VISCA, UDP, presets,
replays, the debug page); a preset still moves pan and tilt. The lens
wizard unlocks it while it is open (renewed every 20 s, locked again 60 s
after the page is closed). With a lens selected the zoom stays between
that lens's two ends: select the lens actually fitted. The pulse is generated by the RP2040's hardware PWM:
this needs the **firmware v2**, reflash the board once (`firmware/flash.sh`,
see docs/raspberry-pi-setup.md). With an older firmware the zoom shows as
unavailable and pan / tilt work as before. The stepper zoom config used
before is kept in `config/backups/`.

## Useful axis options

| Option | Effect |
|---|---|
| `enabled: False` | Axis ignored entirely (hardware not fitted) |
| `active: False` | Axis off at startup: driver unpowered, left out of Home all, jogs, presets and replays. Switched on live with its checkbox (zoom: under the zoom rocker). The choice is remembered on the Pi. |
| `park_position: 0` | Where the axis goes once homed. Default 0 when 0 is within the travel; `none` stays where homing ended. |
| `home_with_all: False` | Axis skipped by "Home all" and default homing, still homable alone |
| `endstop_guard: auto` | MCU stops the axis on its switch outside homing, only if the switch is at the end of travel |
