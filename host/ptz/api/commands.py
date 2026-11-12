"""JSON command set, shared by WebSocket, REST and UDP front-ends.

Every command is a JSON object with a "cmd" key. Optional "id" is echoed in
the reply so clients can match answers.

Motion
    {"cmd": "jog", "pan": 0.5, "tilt": -0.2, "zoom": 0}     normalized -1..1
    {"cmd": "jog", "pan": 1, "scale": {"pan": 0.2}}          speed factor after the curve
    {"cmd": "goto", "pan": 10, "tilt": 5, "speed": 0.5}      user units
    "wait": true on goto / move_rel / home / preset_recall / play / track_arm:
    the reply comes when the motion is finished (or failed). See docs/api.md.
    {"cmd": "move_rel", "pan": -2}
    {"cmd": "stop"}            {"cmd": "estop"}      {"cmd": "clear_estop"}
    {"cmd": "home", "axes": ["pan", "tilt"]}  (omitted: axes with home_with_all)
    {"cmd": "enable", "on": true}
    {"cmd": "axis_active", "axis": "zoom", "on": true}   switch an axis on / off live
    {"cmd": "raw_move", "axis": "zoom", "steps": -3200, "speed": 800}
        wiring check: microsteps at steps/s, no homing, soft limits off (axis unhomed)
Soft limits (learned)
    {"cmd": "limit_set", "axis": "tilt", "side": "max"}   current position (or "value")
    {"cmd": "limit_clear", "axis": "tilt", "side": "max"}  (both: omit side; all: omit axis)
    {"cmd": "limits_enable", "on": true}
Base levelling (mount standing at an angle: jog pan / tilt in a level frame)
    {"cmd": "level_set", "pitch": 3.0, "roll": -1.5}   tilt that looks level at pan 0 / pan 90
    {"cmd": "level_enable", "on": true}
    {"cmd": "level_mark"}     the camera looks level now: learn pitch / roll from the marks
    {"cmd": "level_clear"}    forget the marks, pitch = roll = 0
Zoom lenses (the zoom drive turns the lens's zoom ring)
    {"cmd": "lens_save", "name": "Sony 70-200", "focal_min": 70, "focal_max": 200,
     "at": "tele"}           learning: zoom homed, then moved to the other end of the
                             ring; "at": the end it is at now ("tele" or "wide")
    {"cmd": "lens_select", "name": "Sony 70-200"}   (null: none)   {"cmd": "lens_delete", "name": "..."}
    "points": [[12.5, 35], [20.1, 50]]   optional marks: [zoom position, focal]
    {"cmd": "zoom_focal", "focal": 135}     zoom to a focal length (mm), lens selected
    {"cmd": "goto", "pan": 10, "focal": 85}  same, with other axes in one move
    {"cmd": "lens_tilt_adjust", "on": true}  tilt free (soft limits off) to find the max
    {"cmd": "lens_tilt_max"}         max tilt with this lens = current tilt ("value": x;
                                     "clear": true -> soft limits again)
    With a lens selected, a positive zoom jog always zooms in, whatever the
    direction of its ring, and the zoom stays between the ring's two ends.
    Servo zoom with no lens selected: the zoom is locked (no motion at all).
    {"cmd": "lens_learn", "on": true}   learning a lens: zoom free over its travel
                                        for 60 s (send again to renew; off: lock)
Shutdown
    {"cmd": "shutdown"}        rest position, motors off, files flushed, Pi powered off
    {"cmd": "shutdown", "poweroff": false}   same, but the Pi stays on (safe to unplug)
Settings (axis units, stored on the Pi)
    {"cmd": "set_motion", "speed": 30, "accel": 50, "smoothing": 0.3}   all axes
    {"cmd": "set_motion", "advanced": true,
     "axis_speed": {"pan": 40, "zoom": 3}, "axis_accel": {"tilt": 20}}  per axis
Presets
    {"cmd": "preset_save", "preset": 1, "name": "Stage"}
    {"cmd": "preset_recall", "preset": 1, "speed": 0.8}
    {"cmd": "preset_delete", "preset": 1}      {"cmd": "presets"}
Recording / replay
    {"cmd": "record_start", "mode": "continuous" | "keypoints", "name": "opt",
     "on_move": true}         start the clock on the first movement, trim idle end
    {"cmd": "record_keypoint"}   {"cmd": "record_stop"}   {"cmd": "record_cancel"}
    {"cmd": "recordings"}        {"cmd": "recording_delete", "name": "..."}
    {"cmd": "recording_rename", "name": "...", "new": "..."}
    {"cmd": "play", "name": "...", "speed": 1.0, "loop": false}
    {"cmd": "play_set", "speed": 2.0, "loop": true}   {"cmd": "play_stop"}
Race tracking
    {"cmd": "record_start", "mode": "laps", "name": "Circuit"}   learning session
    {"cmd": "lap_mark"}            car on the start line (Space in the web UI)
    {"cmd": "record_stop"}         -> averaged track "Circuit" (+ "Circuit (raw)")
    {"cmd": "track_build", "source": "Circuit (raw)", "exclude": [2]}  rebuild
    {"cmd": "track_arm", "name": "Circuit"}      go to the start point and wait
    {"cmd": "track_go"}            timing signal: start the lap now (open to all)
    {"cmd": "track_set", "target_lap": 92.5, "speed": 1.0, "auto_rearm": true,
     "auto_adjust": true, "adjust_tolerance": 0.3}
    track_go during a lap drops it and starts the next one (smooth join)
    {"cmd": "track_abort"}
Several cars (timing software; open to all, like track_go)
    {"cmd": "await_car", "car": 5}   follow car 5: head to the line, wait for its crossing
                                     (learning: drops the lap in progress; tracking: re-arm)
    {"cmd": "lap", "car": 5}         car 5 crosses the line: lap mark / GO if it is the car
                                     followed, lap time kept for every car
    {"cmd": "lap", "car": 5, "position": 1}   same, with its race position (optional)
    {"cmd": "car_name", "car": 5, "name": "Dupont"}    {"cmd": "cars_reset"}
    {"cmd": "cars_set", "cars": [5, 12, {"car": 44, "name": "Dupont"}]}   new car list
    {"cmd": "cars_flush"}            forget every car     {"cmd": "cars"}  the list
    {"cmd": "follow_leader", "on": true}   follow whoever leads (switches at the line)
Blocking mode (web clients only)
    {"cmd": "lock"}   {"cmd": "unlock"}   {"cmd": "unlock", "force": true} (from the Pi)
    While locked, only the owner may send control commands; "stop", "estop"
    and read-only commands stay open to everyone.
Info / debug
    {"cmd": "status"}   {"cmd": "config"}   {"cmd": "diag"}
    {"cmd": "debug", "on": true}      verbose logging (web debug console)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from .. import weblog
from ..mcu import McuError
from ..motion import MotionController, MotionError
from ..recorder import RecorderError

log = logging.getLogger("ptz.api")

_RESERVED = {"cmd", "id", "speed", "wait", "axes", "on"}
_QUIET = {"jog", "status", "config", "presets", "recordings", "diag"}
# allowed to everyone, even when another client holds the lock
_OPEN = {"stop", "estop", "status", "config", "presets", "recordings", "diag",
         "debug", "lock", "unlock",
         "track_go", "lap", "await_car", "cars", "cars_set", "cars_flush", "cars_reset",
         "car_name", "follow_leader"}   # the timing system may fire what the operator armed
_last_jog_log = [0.0]


def _axis_values(ctrl: MotionController, msg: dict) -> Dict[str, float]:
    return {k: float(v) for k, v in msg.items()
            if k not in _RESERVED and k in ctrl.state and v is not None}


def _background(ctrl: MotionController, coro) -> None:
    async def runner():
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            ctrl.last_error = str(e)
            log.error("%s", e)
    asyncio.get_running_loop().create_task(runner())


async def _until(condition, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + float(timeout)
    while not condition():
        if loop.time() > end:
            raise MotionError("timeout while waiting")
        await asyncio.sleep(0.05)


def _describe(msg: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in msg.items() if k not in ("cmd", "id"))


async def dispatch(ctrl: MotionController, msg: Any, source: str = "",
                   client: Optional[str] = None, local: bool = False) -> dict:
    """source: label for the logs. client: web client id (blocking mode).
    local: request comes from the Pi itself (may force-unlock)."""
    if not isinstance(msg, dict) or "cmd" not in msg:
        return {"ok": False, "error": "expected a JSON object with a 'cmd' key"}
    cmd = msg["cmd"]
    tag = f"[{source}] " if source else ""
    if cmd not in _OPEN and not ctrl.may_control(client):
        return _fail(cmd, f"control is locked by {ctrl.lock_label}", quiet=cmd == "jog")
    if cmd not in _QUIET:
        log.info("%s%s %s", tag, cmd, _describe(msg))
    elif cmd == "jog" and weblog.is_debug():
        now = time.monotonic()
        if now - _last_jog_log[0] > 0.5:          # don't flood the console
            _last_jog_log[0] = now
            log.debug("%sjog %s", tag, _describe(msg))
    reply: Dict[str, Any] = {"ok": True}
    wait = bool(msg.get("wait", False))     # reply only once the motion is finished
    try:
        # ------------------------------------------------ motion
        if cmd == "jog":
            refused = ctrl.jog(_axis_values(ctrl, msg), msg.get("scale"))
            if refused:
                return _fail(cmd, f"{', '.join(refused)} not homed: home first", quiet=True)
        elif cmd == "goto":
            targets = _axis_values(ctrl, msg)
            if msg.get("focal") is not None:
                targets["zoom"] = ctrl.focal_to_position(msg["focal"])
            await ctrl.goto(targets, speed=float(msg.get("speed", 1.0)), wait=wait)
        elif cmd == "move_rel":
            await ctrl.move_relative(_axis_values(ctrl, msg),
                                     speed=float(msg.get("speed", 1.0)), wait=wait)
        elif cmd == "stop":
            await ctrl.stop(msg.get("axes"))
        elif cmd == "estop":
            await ctrl.estop()
        elif cmd == "clear_estop":
            await ctrl.clear_estop()
        elif cmd == "home":
            ctrl._check_ready()
            if wait:
                await ctrl.home(msg.get("axes"))
            else:
                _background(ctrl, ctrl.home(msg.get("axes")))
        elif cmd == "enable":
            await ctrl.enable(bool(msg.get("on", True)), msg.get("axes"))
        elif cmd == "raw_move":
            reply["raw"] = await ctrl.raw_move(msg["axis"], msg["steps"], msg["speed"],
                                               msg.get("accel"), wait=wait)
        elif cmd == "axis_active":
            await ctrl.set_axis_active(msg["axis"], bool(msg.get("on", True)))
        # ------------------------------------------------ learned soft limits
        elif cmd == "limit_set":
            reply["value"] = await ctrl.set_user_limit(msg["axis"], msg["side"], msg.get("value"))
        elif cmd == "limit_clear":
            await ctrl.clear_user_limit(msg.get("axis"), msg.get("side"))
        elif cmd == "limits_enable":
            await ctrl.enable_user_limits(bool(msg.get("on", True)))
        # ------------------------------------------------ base levelling
        elif cmd == "level_set":
            ctrl.set_level(msg.get("pitch"), msg.get("roll"))
        elif cmd == "level_enable":
            ctrl.enable_level(bool(msg.get("on", True)))
        elif cmd == "level_mark":
            reply["level"] = ctrl.level_mark()
        elif cmd == "lens_save":
            reply["lens"] = await ctrl.lens_save(msg.get("name"), msg.get("focal_min"),
                                                 msg.get("focal_max"), msg.get("at"),
                                                 msg.get("wide"), msg.get("tele"),
                                                 msg.get("points"), msg.get("rename_from"))
        elif cmd == "lens_select":
            await ctrl.lens_select(msg.get("name"))
        elif cmd == "lens_delete":
            await ctrl.lens_delete(msg.get("name"))
        elif cmd == "lenses":
            reply["lens"] = ctrl.lens_status()
        elif cmd == "lens_learn":
            await ctrl.lens_learn(bool(msg.get("on", True)))
            reply["lens"] = ctrl.lens_status()
        elif cmd == "lens_tilt_adjust":
            await ctrl.lens_tilt_adjust(bool(msg.get("on", True)))
        elif cmd == "lens_tilt_max":
            reply["tilt_max"] = await ctrl.lens_tilt_max(msg.get("value"),
                                                         bool(msg.get("clear", False)))
        elif cmd == "zoom_focal":
            await ctrl.zoom_to_focal(msg["focal"], speed=float(msg.get("speed", 1.0)), wait=wait)
        elif cmd == "level_clear":
            ctrl.level_clear()
        # ------------------------------------------------ shutdown
        elif cmd == "shutdown":
            ctrl.claim_shutdown()
            log.warning("shutdown requested")
            _background(ctrl, ctrl.run_shutdown(poweroff=bool(msg.get("poweroff", True))))
            reply["targets"] = ctrl.shutdown_targets()
        # ------------------------------------------------ settings
        elif cmd == "set_motion":
            ctrl.set_motion(speed=msg.get("speed"), accel=msg.get("accel"),
                            smoothing=msg.get("smoothing"), advanced=msg.get("advanced"),
                            axis_speed=msg.get("axis_speed"), axis_accel=msg.get("axis_accel"))
        # ------------------------------------------------ blocking mode
        elif cmd == "lock":
            if client is None:
                raise MotionError("only web clients can take the lock")
            ctrl.lock(client, source or client)
        elif cmd == "unlock":
            ctrl.unlock(client, force=bool(msg.get("force")) and local)
        # ------------------------------------------------ presets
        elif cmd == "preset_save":
            ctrl.save_preset(msg["preset"], msg.get("name", ""))
            reply["presets"] = ctrl.presets.all()
        elif cmd == "preset_recall":
            await ctrl.recall_preset(msg["preset"], speed=float(msg.get("speed", 1.0)),
                                     wait=wait)
        elif cmd == "preset_delete":
            ctrl.presets.delete(msg["preset"])
            reply["presets"] = ctrl.presets.all()
        elif cmd == "presets":
            reply["presets"] = ctrl.presets.all()
        # ------------------------------------------------ recording / replay
        elif cmd == "record_start":
            ctrl.record_start(msg.get("mode", "continuous"), msg.get("name", ""),
                              bool(msg.get("on_move", False)))
        elif cmd == "record_keypoint":
            reply["keypoints"] = ctrl.record_keypoint()
        elif cmd == "record_stop":
            reply["recording"] = ctrl.record_stop()
            reply["recordings"] = ctrl.recorder.list()
        elif cmd == "record_cancel":
            ctrl.recorder.cancel()
        elif cmd == "recordings":
            reply["recordings"] = ctrl.recorder.list()
        elif cmd == "recording_delete":
            ctrl.recorder.delete(msg["name"])
            reply["recordings"] = ctrl.recorder.list()
        elif cmd == "recording_rename":
            ctrl.recorder.rename(msg["name"], msg["new"])
            reply["recordings"] = ctrl.recorder.list()
        elif cmd == "play":
            await ctrl.play(msg["name"], speed=msg.get("speed"), loop=msg.get("loop"))
            if wait:                       # until the replay ends (not with loop)
                await _until(lambda: ctrl.playback is None, msg.get("timeout", 3600))
        elif cmd == "play_set":
            ctrl.set_play(speed=msg.get("speed"), loop=msg.get("loop"))
        elif cmd == "play_stop":
            await ctrl.play_stop()
        # ------------------------------------------------ race tracking
        elif cmd == "lap_mark":
            reply["marks"] = ctrl.lap_mark()
        elif cmd == "track_build":
            reply["recording"] = ctrl.build_track(msg["source"], msg.get("exclude", []),
                                                  msg.get("name", ""))
            reply["recordings"] = ctrl.recorder.list()
        elif cmd == "track_arm":
            await ctrl.track_arm(msg["name"])
            if wait:                       # until the head waits at the start point
                await _until(lambda: ctrl.tracking["phase"] in ("armed", "idle"),
                             msg.get("timeout", 120))
                if ctrl.tracking["phase"] != "armed":
                    raise MotionError("tracking was stopped before being armed")
        elif cmd == "track_go":
            ctrl.track_go()
        elif cmd == "track_set":
            ctrl.track_set(speed=msg.get("speed"), target_lap=msg.get("target_lap"),
                           auto_rearm=msg.get("auto_rearm"), auto_adjust=msg.get("auto_adjust"),
                           adjust_tolerance=msg.get("adjust_tolerance"))
        elif cmd == "track_abort":
            await ctrl.track_abort()
        # ------------------------------------------------ several cars
        elif cmd == "await_car":
            reply["state"] = await ctrl.await_car(msg["car"], msg.get("track"))
        elif cmd == "lap":
            reply.update(ctrl.car_lap(msg.get("car"), msg.get("position")))
        elif cmd == "car_name":
            ctrl.set_car_name(msg["car"], msg.get("name", ""))
        elif cmd == "cars_reset":
            ctrl.cars_reset()
        elif cmd == "cars_set":
            reply["cars"] = ctrl.cars_set(msg.get("cars"))
        elif cmd == "cars_flush":
            ctrl.cars_flush()
        elif cmd == "cars":
            reply["cars"] = ctrl.cars_status()
        elif cmd == "follow_leader":
            reply.update(await ctrl.set_follow_leader(msg.get("on", True)))
        # ------------------------------------------------ info / debug
        elif cmd == "status":
            reply["status"] = ctrl.status()
        elif cmd == "config":
            reply["config"] = config_summary(ctrl)
        elif cmd == "diag":
            reply["diag"] = await ctrl.diagnostics()
        elif cmd == "debug":
            weblog.set_debug(bool(msg.get("on", True)))
            log.info("debug mode %s", "ON" if weblog.is_debug() else "off")
        else:
            return _fail(cmd, f"unknown command '{cmd}'")
    except (MotionError, McuError, RecorderError, KeyError, ValueError, TypeError) as e:
        return _fail(cmd, f"missing parameter {e}" if isinstance(e, KeyError) else str(e))
    return reply


def _fail(cmd: str, error: str, quiet: bool = False) -> dict:
    if quiet:
        log.debug("%s rejected: %s", cmd, error)
    else:
        log.warning("%s rejected: %s", cmd, error)
    return {"ok": False, "error": error}


def config_summary(ctrl: MotionController) -> dict:
    return {
        "axes": [{
            "name": a.name, "index": a.index, "units": a.units,
            "min": a.position_min, "max": a.position_max,
            "max_velocity": a.max_velocity, "max_accel": a.max_accel,
            "jog_velocity": a.jog_velocity,
            "has_endstop": a.has_endstop, "steps_per_unit": a.steps_per_unit,
            "microsteps": a.microsteps, "full_steps": a.full_steps_per_rotation,
            "gear_ratio": a.gear_ratio, "rotation_distance": a.rotation_distance,
            "max_accel_steps": a.max_accel * a.steps_per_unit,
            "max_velocity_steps": a.max_velocity * a.steps_per_unit,
            "servo": a.servo, "pulse_min": a.pulse_min, "pulse_max": a.pulse_max,
            "homing_positive_dir": a.homing_positive_dir,
            "endstop_guard": a.has_endstop and a.endstop_guard,
            "run_current": a.tmc.run_current if a.tmc is not None else None,
            "home_with_all": a.home_with_all,
        } for a in ctrl.axes],
        "jog_timeout": ctrl.cfg.motion.jog_timeout,
        "ease_time": ctrl.cfg.motion.ease_time,
        "disabled_axes": ctrl.cfg.disabled_axes,
    }
