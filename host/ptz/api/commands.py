"""JSON command set, shared by WebSocket, REST and UDP front-ends.

Every command is a JSON object with a "cmd" key. Optional "id" is echoed in
the reply so clients can match answers. Examples:

    {"cmd": "jog", "pan": 0.5, "tilt": -0.2, "zoom": 0}     normalized -1..1
    {"cmd": "goto", "pan": 10, "tilt": 5, "speed": 0.5}      user units
    {"cmd": "move_rel", "pan": -2}
    {"cmd": "stop"}            {"cmd": "estop"}      {"cmd": "clear_estop"}
    {"cmd": "home", "axes": ["pan", "tilt"]}         (all axes if omitted)
    {"cmd": "enable", "on": true}
    {"cmd": "set_speed", "value": 0.5}               global speed factor
    {"cmd": "preset_save", "preset": 1, "name": "Stage"}
    {"cmd": "preset_recall", "preset": 1, "speed": 0.8}
    {"cmd": "preset_delete", "preset": 1}
    {"cmd": "presets"}   {"cmd": "status"}   {"cmd": "config"}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from ..motion import MotionController, MotionError
from ..mcu import McuError

log = logging.getLogger("ptz.api")

_RESERVED = {"cmd", "id", "speed", "wait", "axes", "on"}


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


async def dispatch(ctrl: MotionController, msg: Any) -> dict:
    if not isinstance(msg, dict) or "cmd" not in msg:
        return {"ok": False, "error": "expected a JSON object with a 'cmd' key"}
    cmd = msg["cmd"]
    reply: Dict[str, Any] = {"ok": True}
    try:
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
        elif cmd == "set_speed":
            ctrl.speed = max(0.01, min(1.0, float(msg["value"])))
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
        elif cmd == "status":
            reply["status"] = ctrl.status()
        elif cmd == "config":
            reply["config"] = config_summary(ctrl)
        else:
            return {"ok": False, "error": f"unknown command '{cmd}'"}
    except (MotionError, McuError, KeyError, ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}
    return reply


def config_summary(ctrl: MotionController) -> dict:
    return {
        "axes": [{
            "name": a.name, "index": a.index,
            "min": a.position_min, "max": a.position_max,
            "max_velocity": a.max_velocity, "jog_velocity": a.jog_velocity,
            "has_endstop": a.has_endstop, "steps_per_unit": a.steps_per_unit,
        } for a in ctrl.axes],
        "jog_timeout": ctrl.cfg.motion.jog_timeout,
    }
