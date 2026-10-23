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
 "lock": null, "sources": [], "playback": null, "recording": {"active": false},
 "tracking": {"phase": "idle", "track": null, "lap_time": null, "runs": 0, ...}}
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
| `settings` | Motion settings, see `set_motion`. `effective_*` are the values actually applied per axis, after the per-axis maxima. |
| `lock` | `null`, or `{"owner": <token>, "label": "<ip> (<client>)"}` |
| `sources` | UDP / VISCA senders seen in the last 10 s |
| `playback` | `null`, or `{"name", "phase": "positioning"/"playing", "progress": 0..1, "loop", "pass", "duration"}` |
| `recording` | `{"active": false}`, or `{"active": true, "armed", "mode", "points", "elapsed"}`, plus `marks`, `laps`, `lap_elapsed` and `last_lap` in laps mode |
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

## 6. Blocking mode for live events

When a program holds the lock:

* only that token may send **control** commands;
* everyone else, including web pages, UDP, REST and VISCA, can still send
  the **open** commands: `stop`, `estop`, `track_go`, `status`, `config`,
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
  (0x0000 to 0x4000 maps to the full zoom range);
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
| `stop()`, `estop()`, `clear_estop()`, `set_motion(**settings)`, `axis_active(axis, on)` | |
| `track_arm(name, wait=True)`, `track_go()`, `wait_lap()`, `track_set(**settings)`, `track_abort()` | Race tracking |
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
