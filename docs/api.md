# PTZ mount: control API

This document describes how a program (for example the main script of a
live event) drives the mount without the web page. Everything the web page
does goes through this API, so a script can do all of it too.

* Mount address: `PTZMount.local` (IP `192.168.1.171` on the current
  network), port `8080`.
* Ready-made Python client: [clients/ptz_client.py](../clients/ptz_client.py).
  A complete example script is in
  [clients/live_script_example.py](../clients/live_script_example.py).

## 1. Which interface?

| Interface | Address | Use it for |
|---|---|---|
| **WebSocket** (recommended) | `ws://PTZMount.local:8080/ws` | A main script. One permanent connection, replies to every request, position and status pushed 20 times per second, jog with low latency. |
| JSON over UDP | `PTZMount.local:9000` | Fire-and-forget triggers with the lowest latency, such as the timing loop sending `track_go`, or a hardware joystick. No connection to keep. |
| REST (HTTP) | `http://PTZMount.local:8080/api/...` | One-off commands from tools or `curl`, and file transfers (recordings, configuration). |
| VISCA over IP | UDP `52381` | Standard PTZ keyboards, Bitfocus Companion, OBS, vMix. Basic subset only, see section 10. |

All interfaces share **the same commands** (section 5). Only the transport
differs.

## 2. Conventions

* **Messages** are JSON objects, encoded as UTF-8 text.
* **Axes** are named `pan`, `tilt` and `zoom` (and `focus` if it is enabled
  in the configuration).
* **Units** are the axis units of the configuration: degrees for pan and
  tilt. Positions are in `°`, speeds in `°/s`, accelerations in `°/s²`.
* **Homing first**: an axis must be homed before it can move at all. Homing
  itself is always allowed. Homing is lost when the motors are released, the
  board reboots, or the daemon restarts.
* **Soft limits**: target positions are clipped to each axis's
  `position_min` / `position_max`. Jogging slows down smoothly before a
  limit.
* **Inactive axes**: an axis switched off (`axis_active`, currently zoom by
  default) is unpowered. Jogs, presets, replays and tracking leave it out
  silently. An explicit move or homing of that axis alone is refused.
* **Speed of moves**: `goto`, `move_rel`, presets and the move to the start
  of a replay use the current speed, acceleration and ease settings
  (`set_motion`). Their optional `speed` parameter is a fraction (0 to 1) of
  that speed. All axes of a move start and arrive together.

## 3. WebSocket

### 3.1 Connecting

```
ws://PTZMount.local:8080/ws?token=mainscript01
```

`token` identifies your program. It is optional, but needed for blocking
mode (section 6). Use 8 to 40 letters or digits, and keep the same token
when you reconnect, so your program keeps control of the mount.

Right after connecting, the mount sends these messages, in this order:
`config`, `presets`, `recordings`, `debug`, `clients`. A `status` message
then arrives 20 times per second for as long as the connection is open.

The mount sends a WebSocket ping every 5 s. A client that doesn't answer
with a pong is disconnected. All usual WebSocket libraries answer pings
automatically, as long as your program keeps reading the socket.

### 3.2 Requests and replies

A request is a command object (section 5). Add an `id` field to get a reply.
The `id` can be any JSON value, and it is echoed back.

```json
{"cmd": "goto", "pan": 30, "tilt": -5, "id": 12}
```

Replies (captured from the mount):

```json
{"ok": true, "type": "reply", "id": 12, "cmd": "goto"}
{"ok": false, "error": "axis 'pan' is not homed", "type": "reply", "id": 100, "cmd": "goto"}
{"ok": false, "error": "unknown command 'foo'", "type": "reply", "id": 102, "cmd": "foo"}
{"ok": true, "presets": {...}, "type": "reply", "id": 101, "cmd": "presets"}
```

Rules:

* Without an `id`, the mount replies **only on errors**.
* A rejected `jog` is reported at most once per second, with `"id": null`,
  because jogs are sent many times per second.
* Replies to requests without `wait` come back in the order the requests
  were sent.
* Some replies carry data: `presets`, `recordings`, `status`, `config`,
  `diag`, `keypoints`, `marks`, `recording`. See each command.

### 3.3 Waiting for the end of a motion: `"wait": true`

By default `goto`, `move_rel`, `preset_recall` and `home` reply as soon as
the motion **starts**. Add `"wait": true` to get the reply only when it is
**finished**:

```json
{"cmd": "goto", "pan": 30, "tilt": -5, "wait": true, "id": 13}
```

| Command | With `"wait": true`, the reply comes when... |
|---|---|
| `goto`, `move_rel`, `preset_recall` | the head has arrived and is settled |
| `home` | every requested axis is homed and parked (at 0) |
| `play` | the replay has finished (don't combine with `loop`) |
| `track_arm` | the head is at the start point and armed |

If the motion is interrupted (Stop, E-stop, a jog taking over, another
move), the reply is an error such as `"move interrupted"`.

Requests with `wait` are handled **in the background**. Other commands sent
meanwhile on the same connection, Stop and E-stop in particular, are
processed immediately. Their replies can therefore arrive before the reply of
the waiting request, so match replies by `id`.

### 3.4 Messages pushed by the mount

| `type` | When | Content |
|---|---|---|
| `status` | 20 times per second | Complete state, see section 4 |
| `config` | at connection | Axes and their limits, see below |
| `presets` | at connection, and when presets change | `{"presets": {"1": {"name": ..., "positions": {"pan": .., "tilt": ..}}}}` |
| `recordings` | at connection, and when recordings change | `{"recordings": [summary, ...]}` |
| `clients` | when someone connects, disconnects, or the lock changes | `{"you": "<your token>", "clients": [{"id", "ip", "agent", "since", "owner"}]}` |
| `debug` | when debug mode changes | `{"on": false}` |
| `log`, `log_backlog` | only after `{"cmd": "logs", "on": true}` | The daemon's log messages (debug console) |
| `reply` | see 3.2 | |

The `config` message, as captured:

```json
{"type": "config",
 "axes": [{"name": "pan", "index": 0, "units": "°", "min": -180.0, "max": 180.0,
           "max_velocity": 45.0, "max_accel": 50.0, "jog_velocity": 30.0,
           "has_endstop": true, "steps_per_unit": 150.588, "home_with_all": true},
          ...],
 "jog_timeout": 0.5, "ease_time": 0.6, "disabled_axes": ["focus"]}
```

## 4. Status message

The `status` message is pushed 20 times per second. You can also get it from
`{"cmd": "status"}` (inside the reply) or from `GET /api/status`. Example,
abridged:

```json
{"type": "status", "connected": true, "ready": true, "estop": false,
 "error": "", "config_error": "", "motion": "",
 "axes": {"pan": {"pos": 0.0, "vel": 0.0, "homed": true, "homing": false,
                  "enabled": true, "moving": false, "endstop": false,
                  "at_limit": false, "min": -180.0, "max": 180.0,
                  "units": "°", "active": true}, "tilt": {...}, "zoom": {...}},
 "settings": {"advanced": false, "speed_all": 30.0, "accel_all": 50.0,
              "speed": {...}, "accel": {...},
              "effective_speed": {"pan": 30.0, "tilt": 30.0, "zoom": 5.0},
              "effective_accel": {...}, "smoothing": 0.3, "ease_s": 0.18,
              "play_speed": 1.0, "play_loop": false},
 "limits": {"enabled": true,
            "axes": {"pan": {"min": -35.0, "max": 38.0}, "tilt": {"min": null, "max": 10.0}, ...},
            "effective": {"pan": [-35.0, 38.0], "tilt": [-30.0, 10.0], "zoom": [0.0, 250.0]}},
 "lens": {"current": "Kit 18-135", "sign": 1, "focal": 50.0, "tilt_adjust": false,
          "tilt_range": [-90.0, 32.0],
          "list": [{"name": "Kit 18-135", "focal_min": 18, "focal_max": 135,
                    "wide": 0.0, "tele": 84.5, "points": [[14.2, 35], [22.0, 50], [41.3, 85]],
                    "tilt_max": 24.0}, ...]},
 "level": {"enabled": true, "pitch": 3.0, "roll": -1.5, "marks": [[-30.0, 3.1], [45.0, 1.0]],
           "spread": 75.0, "available": true, "world": {"pan": 12.4, "tilt": 8.0}},
 "shutdown": null,
 "lock": null, "sources": [], "playback": null, "recording": {"active": false},
 "tracking": {"phase": "idle", "track": null, "lap_time": null, "runs": 0, ...},
 "cars": {"current": 44, "waiting": true, "follow_leader": true, "leader": 44, "max": 12,
          "list": [{"car": 44, "name": "", "laps": 4, "last_lap": 92.4, "best_lap": 91.8,
                    "expected": 92.1, "crossings": 5, "position": 1, "since": 31.5}, ...]}}
```

| Field | Meaning |
|---|---|
| `connected` | The board answers. Status from the board is less than 1 s old. |
| `ready` | The board is configured and the mount accepts motion. |
| `estop` | An emergency stop is active. Motion is refused until `clear_estop`. |
| `error` | The last error message, for example a failed homing. It is cleared by the next homing or a cleared E-stop. |
| `config_error` | Set if your edited configuration couldn't be loaded and the default is in use. |
| `motion` | What drives the head now: `""` (idle, or jogging), `"move"` (goto, preset, park), `"playback"`, `"tracking"`. |
| `axes.<n>.pos` / `vel` | Position (units) and speed (units/s), as measured by the board |
| `axes.<n>.homed` / `homing` | Homing done / homing in progress |
| `axes.<n>.enabled` | Driver powered |
| `axes.<n>.moving` | The axis is moving |
| `axes.<n>.endstop` | The switch is pressed |
| `axes.<n>.at_limit` | The axis is held at, or braking toward, a soft limit |
| `axes.<n>.active` | The axis is in use (see `axis_active`) |
| `axes.<n>.steps` | Position in microsteps, as counted by the board |
| `axes.<n>.raw` | The `raw_move` running on this axis (`null`: none): `start`, `target` (microsteps), `speed`, `accel` |
| `axes.<n>.halted` | Stopped by its endstop (endstop guard): it only moves away from the switch |
| `axes.<n>.servo` / `pulse` / `available` | Servo axis (`servo_pin` in the config): no endstop, always homed; `pulse`: current pulse width (us); `available`: false when the board firmware has no servo output (reflash) |
| `mcu_warning` | Board firmware problem the operator must fix (e.g. `"... has no servo output: zoom unavailable. Reflash the SKR Pico ..."`), `""` otherwise. `firmware`: its protocol version. |
| `settings` | Motion settings, see `set_motion`. `effective_*` are the values actually applied per axis, after the per-axis maxima. |
| `limits` | Learned soft limits (5.9). `axes.<n>.min` / `max` are `null` when not set. `effective` is the travel actually allowed: the configured travel, narrowed by the learned limits when `enabled`. |
| `lens` | Zoom lenses (5.12). `current`: the lens fitted (`null`: none). `sign`: +1 when the motor's positive direction zooms in, -1 when it zooms out. `focal`: current focal length in mm (`null` without a lens or before homing). `tilt_adjust`: finding the lens's max tilt (tilt soft limits off). `locked`: servo zoom with no lens, the zoom does not move. `learning`: a lens is being learned (`lens_learn`). `tilt_range`: the configured tilt travel. Per lens: focal range, the zoom positions of its `wide` and `tele` ends, its focal `points` ([zoom position, focal]) and its `tilt_max` (`null`: soft limits). |
| `level` | Base levelling (5.11). `world` is the current aim in the level frame (`null` until pan and tilt are homed). `spread` is the pan range covered by the marks. |
| `shutdown` | `null`, or `{"phase", "message"}` once a shutdown is requested (5.10) |
| `lock` | `null`, or `{"owner": <token>, "label": "<ip> (<client>)"}` |
| `sources` | UDP / VISCA senders seen in the last 10 s |
| `playback` | `null`, or `{"name", "phase": "positioning"/"playing", "progress": 0..1, "loop", "pass", "duration"}` |
| `recording` | `{"active": false}`, or `{"active": true, "armed", "mode", "points", "elapsed"}`, plus `marks`, `laps`, `lap_elapsed` and `last_lap` in laps mode |
| `cars` | Several cars (5.6.1). `list`: the car list, in the order it was sent or seen (12 cars at most). `current`: the car followed (`null`: none, every lap signal counts). `waiting`: the head waits at the line for its next crossing. `follow_leader`: following whoever leads; `leader`: the car leading (`null` before any crossing). Per car: `laps`, `last_lap`, `best_lap` (s), `expected` (smoothed lap time, used as the target lap when switching to that car with auto adjust), `crossings` (since the last reset), `position` (race position, `null` before its first crossing), `since` (s since its last crossing, `null` if never seen). |
| `tracking` | `{"phase": "idle"/"arming"/"armed"/"running"/"done", "track", "lap_time", "current_lap", "speed", "speed_eff", "target_lap", "auto_rearm", "auto_adjust", "adjust_tolerance", "runs", "last_interval", "adjust_note", "progress"}` |

To know when something has finished without `wait`, watch the status:

* a move is finished when `motion` is `""` and no axis is `moving`;
* homing is finished when `homing` is false and `homed` is true;
* a lap is finished when `tracking.runs` has increased.

## 5. Commands

Each command is an object with `cmd` and its parameters. **Open** commands
are accepted from everyone, even when another client holds the lock
(section 6). All the others are **control** commands.

### 5.1 Motion

| Command | Parameters | Effect |
|---|---|---|
| `jog` | `pan`, `tilt`, `zoom`: -1..1; optional `scale` | Continuous move at a speed proportional to the value. See 5.2. |
| `goto` | axis positions (units); `speed` 0..1 (default 1); `wait` | Absolute move. Axes are synchronized and the motion is S-shaped. |
| `move_rel` | axis offsets (units); `speed`; `wait` | Relative move |
| `stop` (open) | `axes` (list, optional) | Controlled stop of the axes. Cancels moves, replays, tracking and jogs. |
| `estop` (open) | - | Instant halt. **Drivers stay powered**, so the tilt keeps holding the camera. Everything is refused until `clear_estop`. |
| `clear_estop` | - | Ends the emergency stop |
| `home` | `axes` (list, optional); `wait` | Homes the axes. By default: every active axis with `home_with_all`. Ends with one combined move to the park positions (0 by default). |
| `enable` | `on` (bool); `axes` (optional) | Powers the drivers on or off. Off releases the motors and **clears homing**. |
| `axis_active` | `axis`, `on` | Switches an axis on or off live (section 2). Switching on requires homing it. The setting is remembered. |
| `raw_move` | `axis`, `steps` (microsteps, signed), `speed` (microsteps/s), `accel` (optional, microsteps/s²), `wait` | Wiring check (the **/debug** page): turns the axis by a number of microsteps **without homing, soft limits or travel limits**. On a servo axis a step is 0.5 us of pulse, the move stays within the configured pulse range and the axis stays homed. The axis loses its homing. Speed and acceleration are capped by the axis's `max_velocity` / `max_accel`. The endstop still halts it when the switch is at the end of the travel (`endstop_guard`), moving toward the homing side; a move further toward the switch is then refused. A move sent while one runs adds to its target. `stop` ends it. The axis must be on and its driver powered. Reply: `raw` (`target`, `speed`, `capped`, and with `wait` the `result`: `"done"`, `"endstop"`, `"stopped"` or `"timeout"`) |

Examples:

```json
{"cmd": "home", "axes": ["pan", "tilt"], "wait": true, "id": 1}
{"cmd": "goto", "pan": 30, "tilt": -5, "speed": 0.5, "wait": true, "id": 2}
{"cmd": "move_rel", "pan": -2, "id": 3}
{"cmd": "goto", "pan": 0, "tilt": 0, "id": 4}
{"cmd": "stop"}
```

### 5.2 Jog rules (important for live control)

* Values go from -1 to 1. The full speed of a value of 1 is the current
  speed setting. The mount applies its deadband (0.04) and response curve
  (expo 0.5) itself, so send raw stick values.
* A jog lasts only as long as it is refreshed. **Send it again at least
  every 0.5 s** (`jog_timeout`). 20 to 50 frames per second gives the best
  feel. An axis whose jog is no longer refreshed comes to a smooth stop. If
  your program, the network or the Pi stops, the board itself stops the
  motion within 0.3 s.
* To stop, send zeros once, or just stop sending.
* **A value of 0 never interrupts a move.** A non-zero value on an axis that
  is in a `goto`, preset, replay or tracked lap **takes over** smoothly
  (manual override), and that motion is cancelled.
* `scale` multiplies the speed after the deadband and curve. It is a number,
  or per axis: `{"pan": 0.2}`. Use it for a speed slider: a slow setting
  still moves the axis instead of falling into the deadband.
* Jogging an unhomed axis is refused (`"pan not homed: home first"`). An
  inactive axis is ignored.
* For continuous camera following, use jog. Don't send a stream of `goto`:
  each `goto` starts a new motion profile from rest.

```json
{"cmd": "jog", "pan": 0.35, "tilt": -0.1}
{"cmd": "jog", "pan": 1, "scale": {"pan": 0.25}}
{"cmd": "jog", "pan": 0, "tilt": 0}
```

### 5.3 Motion settings

| Command | Parameters |
|---|---|
| `set_motion` | `speed` (units/s) and `accel` (units/s²), common to all axes and capped per axis; `smoothing` 0..1 (ease in/out; the ramp time is `smoothing × ease_time`); `advanced` (bool); `axis_speed` / `axis_accel` (`{"pan": 40, ...}`, used in advanced mode) |

Settings are stored on the Pi and survive restarts. The values in effect
are in `status.settings`.

```json
{"cmd": "set_motion", "speed": 30, "accel": 50, "smoothing": 0.3}
{"cmd": "set_motion", "advanced": true, "axis_speed": {"pan": 40, "tilt": 20}}
```

### 5.4 Presets

Preset numbers are integers in requests, but appear as strings (`"1"`) in
the preset lists.

| Command | Parameters | Effect |
|---|---|---|
| `preset_save` | `preset`, `name` (optional) | Stores the current position of every homed axis. The reply contains `presets`. |
| `preset_recall` | `preset`; `speed`; `wait` | Moves to the preset. Inactive axes are left out. |
| `preset_delete` | `preset` | Deletes it |
| `presets` (open) | - | The reply contains `presets` |

### 5.5 Recordings and replay

| Command | Parameters | Effect |
|---|---|---|
| `record_start` | `mode`: `continuous` / `keypoints` / `laps`; `name`; `on_move` (bool) | Starts recording. With `on_move`, the clock starts at the first movement and the motionless end is trimmed. |
| `record_keypoint` | - | Keypoints mode: stores a point. The reply contains `keypoints` (count). |
| `record_stop` | - | Saves. The reply contains `recording` (summary) and `recordings`. |
| `record_cancel` | - | Discards |
| `recordings` (open) | - | The reply contains `recordings` |
| `recording_delete` | `name` | |
| `recording_rename` | `name`, `new` | |
| `play` | `name`; `speed` 0.1..4; `loop`; `wait` | Moves to the start point, then replays |
| `play_set` | `speed`, `loop` | Changes a running replay |
| `play_stop` | - | |

Recording summary: `{"name", "mode", "duration", "points", "axes", "created"}`.
Tracks also have `lap_time`, `laps` and `source`.

### 5.6 Race tracking

| Command | Parameters | Effect |
|---|---|---|
| `record_start` with `mode: "laps"` | `name` | Learning session |
| `lap_mark` | - | The car is on the start / timing line. The reply contains `marks`. |
| `record_stop` | - | Builds the averaged track `name` and keeps the raw session as `name (raw)`. The reply contains `recording` with `lap_time` and `laps` (per lap: `lap`, `duration`, `deviation`, `used`). |
| `track_build` | `source` (the raw session), `exclude` (lap numbers), `name` | Rebuilds the averaged track |
| `track_arm` | `name`; `wait` | Moves to the start point and waits for GO |
| `track_go` (**open**) | - | Starts the lap now. During a lap, it drops that lap and joins the next one smoothly. |
| `track_set` | `target_lap` (s; 0 = learned lap time), `speed`, `auto_rearm`, `auto_adjust`, `adjust_tolerance` (0..1) | |
| `track_abort` | - | Stops tracking |

`track_go` is open on purpose. When your script holds the lock, the timing
system can still fire the lap the script armed, for example by UDP (section 8).

Typical sequence:

```json
{"cmd": "track_set", "auto_rearm": true, "auto_adjust": true, "id": 1}
{"cmd": "track_arm", "name": "Circuit", "wait": true, "id": 2}
{"cmd": "track_go", "id": 3}
```

After `track_go`, the lap is finished when `status.tracking.runs` increases.
With `auto_rearm`, the head goes back to the start point and waits for the
next GO (`phase` `"armed"`).

#### 5.6.1 Several cars (timing software)

The mount does not know which car is which: the timing software tells it
which car the operator follows and sends a `lap` each time a car crosses
the start / finish line. Only the car followed drives the head; the lap
times of every car are kept.

Car numbers are race numbers, 1..999. The mount keeps a list of up to 12
cars: the timing software sends it (`cars_set`), or cars are added as they
are first seen (`await_car`, `lap`, `car_name`). Once 12 cars are listed,
another number is refused (`lap` replies `"unknown"`) until a new list is
sent or the list is flushed. The list and names are kept on the Pi.

| Command | Parameters | Effect |
|---|---|---|
| `await_car` (**open**) | `car` (1..999), `track` (optional) | Follow this car from its next crossing. **Learning:** the lap in progress is dropped (it never becomes a lap), the head goes to where it looked at the line (mean of the marks so far) and waits; the next `lap` of this car starts a new lap. **Tracking** (armed, running or done, or `track` given): re-arms on the track (back to the start point); GO now only comes from this car. With auto adjust, the car's `expected` lap time becomes the target lap. Otherwise the car is only selected. Turns `follow_leader` off. Reply: `state` = `"learning"`, `"tracking"` or `"selected"`. |
| `lap` (**open**) | `car` (optional: the car followed), `position` (optional: its race position after this crossing) | This car crosses the line. If it is the car followed (or no car is followed): a lap mark while learning, a GO while tracking. Lap time kept for every car. A repeat of the same car within 2 s is ignored. Reply: `car`, `action` = `"mark"`, `"go"`, `"none"` (another car, or nothing running), `"ignored"` or `"unknown"` (list full); `switched`: true when this crossing made it the car followed (follow the leader). |
| `follow_leader` (**open**) | `on` (bool, default true) | Follow the leading car, whatever its number (see below). Switching on goes to the leader at once (as `await_car`) if it is known and not followed yet. Reply: `follow_leader`, `leader`, `state` |
| `cars_set` (**open**) | `cars`: list of car numbers, or `{"car": 44, "name": "Dupont"}` objects (12 at most) | New car list. Cars still in the list keep their lap times (and their name unless one is given); the others are forgotten. If the car followed is dropped, none is followed. Reply: `cars` (the numbers) |
| `cars` (**open**) | - | Reply: `cars` (as in the status) |
| `cars_flush` (**open**) | - | Forgets every car: list, names, lap times. None is followed. |
| `car_name` (**open**) | `car`, `name` (24 characters max, `""` to clear) | Name shown in the web UI (kept on the Pi) |
| `cars_reset` (**open**) | - | New race: forgets every car's lap times, crossing counts and positions (the list and names are kept) |

**Follow the leader.** The leader is worked out at the line: the car with
the most crossings since the last `cars_reset`, and between equals the one
that crossed first. If the timing software sends `position` with its
`lap` signals, positions are used instead (the latest report wins). A car
that takes the lead does so by crossing the line first, so the head
switches to it **at that crossing**: while learning, the lap in progress
(the old leader's) is dropped and this crossing starts a new lap; while
tracking, this crossing is the GO (the lap in progress is dropped and the
head glides onto the new one). Send `cars_reset` at the start of the race
so the counts start together. Choosing a car (`await_car`, or a click on
a car) turns it off.

Lap times: a crossing interval longer than 1.6 times the car's usual lap is
not counted (laps were missed, for example the software only reports the
car followed and it was not followed meanwhile). With auto adjust, the
auto adjust of 5.6 uses the interval between two crossings of the **same**
car, so switching cars never produces a wrong lap time.

Timing software sequence, learning a track while following several cars:

```json
{"cmd": "record_start", "mode": "laps", "name": "Circuit"}
{"cmd": "await_car", "car": 5}
{"cmd": "lap", "car": 5}         // each crossing of car 5 (any car: also fine)
{"cmd": "await_car", "car": 2}   // the operator switches: head back to the line
{"cmd": "lap", "car": 2}         // car 2 crosses: a new lap starts
{"cmd": "record_stop"}
```

Race following the leader, every car reported:

```json
{"cmd": "cars_set", "cars": [5, 12, {"car": 44, "name": "Dupont"}]}
{"cmd": "track_arm", "name": "Circuit"}
{"cmd": "cars_reset"}                       // start of the race
{"cmd": "follow_leader", "on": true}
{"cmd": "lap", "car": 12, "position": 1}    // every crossing of every car
{"cmd": "lap", "car": 5, "position": 2}     // (position: optional)
```

Live tracking is the same: `track_arm` once, then `await_car` / `lap`.
The car commands are open like `track_go`: they work while the web page
holds the lock. `record_start` / `record_stop` are control commands (not
open).

### 5.7 Blocking mode

| Command | Parameters | Effect |
|---|---|---|
| `lock` (open) | - | Your token becomes the only one allowed to send control commands |
| `unlock` (open) | `force` | Releases the lock. `force` works only from the Pi itself (localhost). |

### 5.8 Information and diagnostics

| Command | Effect |
|---|---|
| `status` (open) | The reply contains `status` (section 4) |
| `config` (open) | The reply contains `config` |
| `diag` (open) | Endstop states and TMC2209 driver registers per axis |
| `debug` (open), `on` | Verbose logging on the Pi |
| `logs` (WebSocket only), `on` | Subscribes to `log_backlog` and `log` messages |

### 5.9 Soft limits

Learned limits narrow the travel set in the configuration, for example to
keep the camera off a wall or a pit building. When enabled, every motion
stops at them: jog, goto, presets, replays, tracking, from any source. A
replay or a tracked lap that goes past a limit brakes and runs along it. The
board also receives them as a safety net. An axis that is outside its limits
can only move back inside.

| Command | Parameters | Effect |
|---|---|---|
| `limit_set` | `axis`, `side` (`"min"`/`"max"`), `value` (optional) | Sets one limit. Without `value`: the current head position (the axis must be homed). The reply has `value`. |
| `limit_clear` | `axis` (optional), `side` (optional) | Removes one limit, both limits of an axis, or every limit |
| `limits_enable` | `on` (bool) | Enforces the learned limits, or not. The limits stay stored either way. |

On pan, `min` is left and `max` is right; on tilt, `min` is down and `max`
is up. The limits and the on/off switch are stored on the Pi.

```json
{"cmd": "limit_set", "axis": "tilt", "side": "max", "id": 1}
{"cmd": "limit_set", "axis": "pan", "side": "min", "value": -60, "id": 2}
{"cmd": "limits_enable", "on": true, "id": 3}
```

### 5.10 Shutdown

| Command | Parameters | Effect |
|---|---|---|
| `shutdown` | `poweroff` (bool, default true) | Moves the homed axes to their rest position (`shutdown_position` in the configuration: pan 0, tilt at its max end by default), turns the motors off, flushes every file to the SD card, then powers the Raspberry Pi off. The reply comes at once, with `targets` (the rest positions); follow `status.shutdown`. |

`status.shutdown.phase` goes `"parking"` → `"motors_off"` → `"powering_off"`.
The connection then drops: wait about 15 s (the Pi's green LED stops
blinking) before unplugging. If the Pi is not allowed to power itself off
(sudoers rule missing, see `deploy/setup-remote.sh`), or with
`"poweroff": false`, the phase ends as `"safe"`: the motors are off,
everything is written, and the mount can be unplugged. A stop or E-stop
during the rest move cancels the move only: the motors are still turned off.
A second request is refused unless the first one `"failed"`.

### 5.11 Base levelling

When the mount stands at an angle, a pan sweep in the mount's own frame
rises on one side and dips on the other. With levelling enabled, every jog
(web joystick, keyboard, gamepad, UDP, VISCA drive) is done in a level
frame: a pan jog keeps the aim at the same height above the horizon, the
tilt motor following as needed; a tilt jog changes that height. If one axis
reaches a limit, both stop together (the aim stays on the level path).

The base attitude is given in tilt units, with the mount's own tilt sign:

* `pitch`: the tilt reading that looks level when the head is at pan 0
* `roll`: the tilt reading that looks level when the head is at pan +90

| Command | Parameters | Effect |
|---|---|---|
| `level_set` | `pitch`, `roll` (either or both, -45..45) | Sets the base attitude |
| `level_enable` | `on` (bool) | Levelled jog on / off |
| `level_mark` | | The camera looks level right now (its electronic level at 0, or the horizon centred): adds a mark at the current pan / tilt and recomputes `pitch` / `roll` from all the marks. Two marks at least 30° apart in pan are needed; with one, the base is assumed to lean along that pan. A new mark replaces one taken within 5° of pan. Reply: `level` (as in the status). |
| `level_clear` | | Forgets the marks; `pitch` = `roll` = 0 |

Only jogging is corrected. Moves, presets, replays and tracking use mount
positions, so they go back exactly where they were taken. The model assumes
tilt 0 is perpendicular to the pan axis. A pan / tilt head cannot cancel the
roll of the picture itself: only the aim is corrected.

```json
{"cmd": "level_mark", "id": 1}
{"cmd": "level_enable", "on": true, "id": 2}
```

### 5.12 Zoom lenses

The zoom drive turns the lens's zoom ring through a gear: a hobby servo
(`servo_pin`, the default) or a stepper with an endstop. Lenses do not all
turn the same way: on some the low end of the zoom axis is wide, on others
it is tele. A lens profile stores which zoom position is wide and which is
tele, so that:

* a positive zoom jog (web rocker up / T, PgUp, gamepad, UDP
  `{"cmd":"jog","zoom":1}`, VISCA tele) always **zooms in**, whatever the
  lens;
* the zoom stays between the ring's two ends (they become the zoom limits,
  also enforced by the MCU);
* VISCA direct zoom and the gamepad's zoom-position slider go from wide
  (0) to tele (max);
* the focal length is shown instead of the ring angle, and `zoom_focal`
  (or `goto` with `focal`) goes to a focal length. Between the focal marks
  learned on the ring (for an 18-135: 35, 50, 85 mm) the focal length is
  interpolated linearly; without marks, linearly between the two ends;
* a lens may have its own **max tilt** (a long lens hitting the mount when
  the camera tilts down): it replaces the soft limit's tilt max while that
  lens is selected, and never goes past the configured tilt travel.

| Command | Parameters | Effect |
|---|---|---|
| `lens_save` | `name`, `focal_min`, `focal_max` (mm), `at`, `points` (optional), `rename_from` (optional). Servo zoom: no `at`, `points` include the marks at `focal_min` and `focal_max` (the ends) | Learning. The zoom was homed (the ring at the endstop end) and moved to the other end of the ring (`move_rel`, the web wizard's steps, or jog). `at`: the end it is at now, `"tele"` (zoomed in) or `"wide"`. The other end is the endstop position. Stores the lens and selects it (same name: replaced). Instead of `at`, `wide` and `tele` positions can be given directly. `points`: focal marks `[[zoom position, focal], ...]` taken on the way (the focal printed on the barrel), strictly between the ends and in order along the ring. A lens learned again keeps its max tilt; `rename_from`: the old name of a lens being renamed. Reply: `lens` |
| `lens_select` | `name` (`null` or `""`: none) | The lens fitted. Without a lens: motor direction and the configured travel; **servo zoom: locked** (no motion, see below). Ends a lens learning. |
| `lens_learn` | `on` (bool) | Servo zoom: learning a lens, the zoom is free over its configured travel with no lens selected. Lasts 60 s: send it again to renew (the web wizard does, every 20 s). Off, `lens_save` or `lens_select`: ends it (no lens: the zoom stops and is locked where it rests). Reply: `lens` |
| `lens_delete` | `name` | Forgets a lens |
| `lenses` | - | Reply: `lens` (as in the status) |
| `zoom_focal` | `focal` (mm), `speed`, `wait` | Zooms to this focal length (lens selected). `goto` also takes `focal`, to move the zoom with other axes. |
| `lens_tilt_adjust` | `on` (bool) | On: the tilt is free over its configured travel (soft limits and the lens's max tilt off) to find how low the camera may go with this lens. Off: cancel, the limits come back and the tilt moves back inside them. |
| `lens_tilt_max` | `value` (optional, default: the current tilt), `clear` (bool) | Max tilt with the lens selected, never past the configured travel; ends the adjust. `clear`: none, the soft limits apply. Reply: `tilt_max` |

Before learning, select no lens: a lens's range would stop the zoom at its
ends. The configured travel of the zoom axis must cover the longest ring
travel.

**Servo zoom with no lens: locked.** The servo forces its way to the
position asked and could break a lens past its ring's end. With no lens
selected (`lens.locked` true in the status) the MCU limits of the zoom are
pinned where it rests, `goto` / `move_rel` / `zoom_focal` / `raw_move` of
the zoom fail with "zoom locked: no lens selected...", zoom jogs (web,
gamepad, UDP, VISCA) are ignored, and a preset or a move with other axes
moves those axes only. `lens_learn` unlocks it while learning.

**Servo zoom** (no endstop, no homing): start learning, move the zoom
anywhere (`goto`, `move_rel`, jog) and mark each focal length printed on
the barrel. The marks at `focal_min` and `focal_max` are the two ends of
the zoom (`lens_save` without `at`, `wide`, `tele`):

```json
{"cmd": "lens_select", "name": null}
{"cmd": "lens_learn", "on": true}
{"cmd": "goto", "zoom": 22, "wait": true}
{"cmd": "goto", "zoom": 88, "wait": true}
{"cmd": "lens_save", "name": "Kit 18-135", "focal_min": 18, "focal_max": 135,
 "points": [[22, 18], [40, 35], [52, 50], [70, 85], [88, 135]]}
```

**Stepper zoom with an endstop**: homing turns the ring until the endstop,
that end is one end of the ring; step to the other end and say which it is:

```json
{"cmd": "lens_select", "name": null}
{"cmd": "home", "axes": ["zoom"], "wait": true}
{"cmd": "move_rel", "zoom": 10, "wait": true}
{"cmd": "lens_save", "name": "Canon 24-105", "focal_min": 24, "focal_max": 105, "at": "wide"}
```

## 6. Blocking mode for live events

When a program holds the lock:

* only that token may send **control** commands;
* everyone else, including web pages, UDP, REST and VISCA, can still send
  the **open** commands: `stop`, `estop`, `track_go`, the car commands of 5.6.1, `status`, `config`,
  `presets`, `recordings`, `diag`, `debug`, `lock` and `unlock`;
* other web pages show a red "Locked by ..." banner, and their controls are
  disabled;
* the lock is released by `unlock`, or automatically when the **last**
  WebSocket connection with that token closes.

If a program dies while holding the lock, the lock goes away with its
connection. If it ever gets stuck anyway, release it from the Pi:

```sh
curl -X POST localhost:8080/api/cmd -d '{"cmd":"unlock","force":true}'
```

Recommended for a live event:

1. Connect the main script with a fixed token, for example
   `mainscript01`, and send `lock`.
2. Keep the WebSocket open for the whole event. On a disconnection,
   reconnect with the same token and send `lock` again. The Python client
   does both automatically.
3. At start-up, check `ready`, `connected` and `estop`, then home with
   `"wait": true` if `homed` is false.
4. Sequence the run sheet with `"wait": true`. For live following, use a jog
   stream.
5. Let the timing loop send `track_go` by UDP. It is accepted even while the
   script holds the lock.

## 7. Safety behaviour a script should know

| Event | What happens |
|---|---|
| Jog not refreshed within 0.5 s | That axis comes to a smooth stop |
| Script, network or Pi stops sending | The board stops all jogging within 0.3 s (its own watchdog) |
| WebSocket closes while jogging | The jogs from that connection stop immediately |
| `estop` | Instant halt, drivers stay powered; motion refused until `clear_estop` |
| Board reboot | All axes lose homing; current motion is cancelled; the daemon reconfigures the board automatically (`ready` goes false, then true) |
| Daemon restart | Same as a board reboot, plus WebSocket clients are disconnected |
| `enable` off | Motors released, homing lost |

## 8. UDP (port 9000)

Send one JSON command per datagram, encoded as UTF-8. There is no
connection.

* `jog` is handled directly, with no reply. It is the fastest path for a
  joystick.
* Other commands are replied to only if they carry an `id` or fail. The
  reply goes back to the sender's address and port.
* `{"cmd": "subscribe"}` makes the mount send `status` datagrams (20 per
  second) to the sender for 10 s. Send it again regularly to keep receiving.
* UDP has no token, so it can't take the lock. When a web client or script
  holds the lock, UDP is limited to the open commands, such as `track_go`.

```sh
echo '{"cmd":"track_go"}' | nc -u -w0 PTZMount.local 9000
```

```python
import json, socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(json.dumps({"cmd": "track_go"}).encode(), ("PTZMount.local", 9000))
```

## 9. REST (HTTP, port 8080)

| Method and path | Body | Result |
|---|---|---|
| `GET /api/status` | | Status (section 4), without `type` |
| `GET /api/config` | | Axes configuration |
| `GET /api/presets` | | Presets |
| `GET /api/recordings` | | List of recording summaries |
| `GET /api/recordings/{name}` | | Download a recording (JSON file) |
| `POST /api/recordings?name=...` | Recording JSON | Upload a recording (validated; never overwrites) |
| `PUT /api/recordings/{name}` | Recording JSON | Saves an edited recording (validated). **Replaces** an existing one; the previous version is kept in `~/.ptz/recordings/backups/` (last 10). For a track, the lap time becomes the time of the last point. Needs the lock when another client holds it. |
| `POST /api/cmd` | A command object | The reply (status 200 if ok, 400 if refused) |
| `GET /api/config/file` | | The configuration text and file information |
| `POST /api/config/validate` | `{"text": ...}` | Checks a configuration |
| `POST /api/config/file` | `{"text": ...}` | Saves it to `~/.ptz/ptz.cfg` (with a backup) |
| `POST /api/config/reset` | | Back to the repository default |
| `POST /api/restart` | | Restarts the daemon (homing is lost) |

For blocking mode over REST, send your token in an `X-PTZ-Client` header.
`POST /api/cmd` with `"wait": true` keeps the HTTP request open until the
motion is finished.

```sh
curl -s PTZMount.local:8080/api/status
curl -s -X POST PTZMount.local:8080/api/cmd -H "Content-Type: application/json" \
     -d '{"cmd":"goto","pan":20,"tilt":0,"wait":true}'
```

## 10. VISCA over IP (UDP 52381)

This is for off-the-shelf controllers. It accepts the Sony VISCA-over-IP
framing (8-byte header) and raw VISCA.

Supported:
* pan/tilt drive and stop, and absolute pan/tilt in 0.1° units;
* home (moves to 0/0) and reset (runs homing);
* zoom stop, tele and wide (fixed and variable speed), and direct zoom
  (0x0000 to 0x4000 maps to the full zoom range, wide to tele of the lens
  selected, see 5.12);
* preset reset, set and recall;
* pan/tilt and zoom position inquiries.

## 11. Python client

[clients/ptz_client.py](../clients/ptz_client.py) is a single file. It needs
`pip install aiohttp` and Python 3.9 or later. Copy it next to your script.

```python
import asyncio
from ptz_client import PTZClient, PTZError, JogStream

async def main():
    async with PTZClient("PTZMount.local", token="mainscript01") as ptz:
        await ptz.lock()
        if not ptz.is_homed("pan", "tilt"):
            await ptz.home(["pan", "tilt"], wait=True)
        await ptz.preset(1, wait=True)
        await ptz.goto(pan=25, tilt=-5, wait=True)
        async with JogStream(ptz) as jog:      # resent at 25 Hz for you
            jog.set(pan=0.4)
            await asyncio.sleep(2)
        await ptz.track_arm("Circuit", wait=True)
        await ptz.track_go()
        await ptz.wait_lap()
        await ptz.unlock()

asyncio.run(main())
```

| Member | Purpose |
|---|---|
| `PTZClient(host, port=8080, token=None, reconnect=True, timeout=5.0)` | Client. Use it as `async with`, or call `connect()` / `close()`. |
| `status`, `config`, `presets`, `recordings` | Latest values received from the mount |
| `on_status`, `on_message` | Callbacks for every status push or every message |
| `call(cmd, timeout=None, **params)` | Any command. Returns the reply, raises `PTZError` if refused. |
| `send(cmd, **params)` | Fire and forget (used for jog) |
| `wait_status(predicate, timeout)` | Waits until `predicate(status)` is true |
| `position(axis)`, `positions()`, `is_homed(*axes)` | Status helpers |
| `lock()`, `unlock()` | Blocking mode. The lock is taken again automatically after a reconnection. |
| `home(axes, wait=True)`, `goto(..., speed, wait)`, `move_rel(...)`, `preset(n, speed, wait)`, `save_preset(n, name)` | Motion |
| `jog(**values)`, `JogStream(client, rate=25)` | Jog: one frame, or a continuous stream |
| `stop()`, `estop()`, `clear_estop()`, `set_motion(**settings)`, `axis_active(axis, on)`, `raw_move(axis, steps, speed, accel=None, wait=False)` | |
| `limit_set(axis, side, value=None)`, `limit_clear(axis=None, side=None)`, `limits_enable(on)` | Soft limits |
| `level_set(pitch=None, roll=None)`, `level_enable(on)`, `level_mark()`, `level_clear()` | Base levelling (5.11) |
| `lens_save(name, focal_min, focal_max, at, points=None)`, `lens_select(name)`, `lens_delete(name)`, `lenses()`, `zoom_focal(focal, wait=False)`, `lens_tilt_adjust(on)`, `lens_tilt_max(value=None, clear=False)`, `lens_learn(on=True)` | Zoom lenses (5.12) |
| `shutdown(poweroff=True)` | Rest, motors off, power off (5.10) |
| `track_arm(name, wait=True)`, `track_go()`, `wait_lap()`, `track_set(**settings)`, `track_abort()` | Race tracking |
| `await_car(car, track=None)`, `lap(car=None, position=None)`, `follow_leader(on=True)`, `cars_set(cars)`, `cars()`, `cars_flush()`, `car_name(car, name)`, `cars_reset()` | Several cars (5.6.1) |
| `play(name, speed, loop, wait)`, `play_stop()` | Replay |

If the connection drops, pending calls fail with `ConnectionError`, and the
client reconnects every second until it succeeds.

### Other languages

Any WebSocket library works. For example in JavaScript (Node 22+ or a
browser):

```js
const ws = new WebSocket("ws://PTZMount.local:8080/ws?token=mainscript01");
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.type === "reply") console.log("reply", m.id, m.ok, m.error);
  if (m.type === "status") console.log("pan", m.axes.pan.pos);
};
ws.onopen = () => ws.send(JSON.stringify({ cmd: "goto", pan: 10, wait: true, id: 1 }));
```

For manual tests, `websocat ws://PTZMount.local:8080/ws` opens an
interactive session where you type the JSON commands.

## 12. Common errors

| Error | Meaning |
|---|---|
| `axis 'pan' is not homed` / `pan not homed: home first` | Home the axis first |
| `zoom is disabled` | The axis is switched off. Use `axis_active`. |
| `control is locked by <ip> (<client>)` | Another token holds the lock |
| `emergency stop active` | Send `clear_estop` |
| `MCU not ready` | The board isn't configured yet, or it is rebooting |
| `move interrupted` | A `wait` motion was cancelled by Stop, E-stop, a jog or another move |
| `tracking is not armed: select a track and press ARM` | `track_go` without `track_arm` |
| `missing parameter 'name'` | A required parameter is missing |
| `unknown command 'x'` | Check the spelling, see section 5 |
