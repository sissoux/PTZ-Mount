"""JSON command set, shared by WebSocket, REST and UDP front-ends.

Every command is a JSON object with a "cmd" key. Optional "id" is echoed in
the reply so clients can match answers.

Motion
    {"cmd": "jog", "pan": 0.5, "tilt": -0.2, "zoom": 0}     normalized -1..1
    {"cmd": "goto", "pan": 10, "tilt": 5, "speed": 0.5}      user units
    {"cmd": "move_rel", "pan": -2}
    {"cmd": "stop"}            {"cmd": "estop"}      {"cmd": "clear_estop"}
    {"cmd": "home", "axes": ["pan", "tilt"]}  (omitted: axes with home_with_all)
    {"cmd": "enable", "on": true}
Settings (fractions 0..1, stored on the Pi)
    {"cmd": "set_motion", "speed": 0.8, "accel": 0.5, "smoothing": 0.3}
    {"cmd": "set_speed", "value": 0.5}                       (legacy)
Presets
    {"cmd": "preset_save", "preset": 1, "name": "Stage"}
    {"cmd": "preset_recall", "preset": 1, "speed": 0.8}
    {"cmd": "preset_delete", "preset": 1}      {"cmd": "presets"}
Recording / replay
    {"cmd": "record_start", "mode": "continuous" | "keypoints", "name": "opt"}
    {"cmd": "record_keypoint"}   {"cmd": "record_stop"}   {"cmd": "record_cancel"}
    {"cmd": "recordings"}        {"cmd": "recording_delete", "name": "..."}
    {"cmd": "recording_rename", "name": "...", "new": "..."}
    {"cmd": "play", "name": "...", "speed": 1.0, "loop": false}
    {"cmd": "play_set", "speed": 2.0, "loop": true}   {"cmd": "play_stop"}
Info / debug
    {"cmd": "status"}   {"cmd": "config"}   {"cmd": "diag"}
    {"cmd": "debug", "on": true}      verbose logging (web debug console)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict

from .. import weblog
from ..mcu import McuError
from ..motion import MotionController, MotionError
from ..recorder import RecorderError

log = logging.getLogger("ptz.api")

_RESERVED = {"cmd", "id", "speed", "wait", "axes", "on"}
_QUIET = {"jog", "status", "config", "presets", "recordings", "diag"}
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


def _describe(msg: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in msg.items() if k not in ("cmd", "id"))


async def dispatch(ctrl: MotionController, msg: Any, source: str = "") -> dict:
    if not isinstance(msg, dict) or "cmd" not in msg:
        return {"ok": False, "error": "expected a JSON object with a 'cmd' key"}
    cmd = msg["cmd"]
    tag = f"[{source}] " if source else ""
    if cmd not in _QUIET:
        log.info("%s%s %s", tag, cmd, _describe(msg))
    elif cmd == "jog" and weblog.is_debug():
        now = time.monotonic()
        if now - _last_jog_log[0] > 0.5:          # don't flood the console
            _last_jog_log[0] = now
            log.debug("%sjog %s", tag, _describe(msg))
    reply: Dict[str, Any] = {"ok": True}
    try:
        # ------------------------------------------------ motion
        if cmd == "jog":
            ctrl.jog(_axis_values(ctrl, msg))
        elif cmd == "goto":
            await ctrl.goto(_axis_values(ctrl, msg), speed=float(msg.get("speed", 1.0)))
        elif cmd == "move_rel":
            await ctrl.move_relative(_axis_values(ctrl, msg),
                                     speed=float(msg.get("speed", 1.0)))
        elif cmd == "stop":
            await ctrl.stop(msg.get("axes"))
        elif cmd == "estop":
            await ctrl.estop()
        elif cmd == "clear_estop":
            await ctrl.clear_estop()
        elif cmd == "home":
            ctrl._check_ready()
            _background(ctrl, ctrl.home(msg.get("axes")))
        elif cmd == "enable":
            await ctrl.enable(bool(msg.get("on", True)), msg.get("axes"))
        # ------------------------------------------------ settings
        elif cmd == "set_motion":
            ctrl.set_motion(speed=msg.get("speed"), accel=msg.get("accel"),
                            smoothing=msg.get("smoothing"))
        elif cmd == "set_speed":
            ctrl.set_motion(speed=msg["value"])
        # ------------------------------------------------ presets
        elif cmd == "preset_save":
            ctrl.save_preset(msg["preset"], msg.get("name", ""))
            reply["presets"] = ctrl.presets.all()
        elif cmd == "preset_recall":
            await ctrl.recall_preset(msg["preset"], speed=float(msg.get("speed", 1.0)))
        elif cmd == "preset_delete":
            ctrl.presets.delete(msg["preset"])
            reply["presets"] = ctrl.presets.all()
        elif cmd == "presets":
            reply["presets"] = ctrl.presets.all()
        # ------------------------------------------------ recording / replay
        elif cmd == "record_start":
            ctrl.record_start(msg.get("mode", "continuous"), msg.get("name", ""))
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
        elif cmd == "play_set":
            ctrl.set_play(speed=msg.get("speed"), loop=msg.get("loop"))
        elif cmd == "play_stop":
            await ctrl.play_stop()
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


def _fail(cmd: str, error: str) -> dict:
    log.warning("%s rejected: %s", cmd, error)
    return {"ok": False, "error": error}


def config_summary(ctrl: MotionController) -> dict:
    return {
        "axes": [{
            "name": a.name, "index": a.index,
            "min": a.position_min, "max": a.position_max,
            "max_velocity": a.max_velocity, "jog_velocity": a.jog_velocity,
            "has_endstop": a.has_endstop, "steps_per_unit": a.steps_per_unit,
            "home_with_all": a.home_with_all,
        } for a in ctrl.axes],
        "jog_timeout": ctrl.cfg.motion.jog_timeout,
        "ease_time": ctrl.cfg.motion.ease_time,
        "disabled_axes": ctrl.cfg.disabled_axes,
    }
