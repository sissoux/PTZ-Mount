"""End-to-end tests: MotionController <-> binary protocol <-> simulated MCU."""
import asyncio
import json

import pytest

from ptz import config as C
from ptz.api.commands import dispatch
from ptz.api.visca import ViscaServer
from ptz.mcu import McuLink
from ptz.motion import MotionController, MotionError
from ptz.sim import SimTransport

FAST_CFG = """
[mcu]
status_rate: 100
watchdog_timeout: 0.2
[server]
state_dir: {state}
[motion]
jog_timeout: 0.3
expo: 0
deadband: 0
[axis pan]
step_pin: gpio11
dir_pin: gpio10
enable_pin: !gpio12
endstop_pin: ^gpio4
microsteps: 16
rotation_distance: 360
gear_ratio: 80:16
position_min: -170
position_max: 170
position_endstop: -175
homing_speed: 200
second_homing_speed: 50
homing_retract_dist: 2
max_velocity: 400
max_accel: 4000
jog_velocity: 100
[axis tilt]
step_pin: gpio6
dir_pin: gpio5
endstop_pin: ^gpio3
microsteps: 16
rotation_distance: 360
position_min: -45
position_max: 90
position_endstop: -45
homing_speed: 200
second_homing_speed: 50
homing_retract_dist: 2
max_velocity: 400
max_accel: 4000
[axis zoom]
step_pin: gpio19
dir_pin: gpio28
microsteps: 16
rotation_distance: 360
position_min: 0
position_max: 90
position_endstop: 0
max_velocity: 400
max_accel: 4000
"""


def run(coro_fn, tmp_path, cfg_text=None):
    async def main():
        p = tmp_path / "t.cfg"
        text = cfg_text if cfg_text is not None else FAST_CFG
        p.write_text(text.format(state=str(tmp_path).replace("\\", "/")))
        cfg = C.load(str(p))
        assert cfg.warnings == []
        ctrl = MotionController(cfg, McuLink(SimTransport()))
        await ctrl.start()
        for _ in range(100):
            if ctrl.ready and ctrl.connected:
                break
            await asyncio.sleep(0.05)
        assert ctrl.ready
        try:
            await asyncio.wait_for(coro_fn(ctrl), 30)
        finally:
            await ctrl.close()
    asyncio.run(main())


def test_home_and_goto(tmp_path):
    async def t(ctrl):
        with pytest.raises(MotionError):
            await ctrl.goto({"pan": 10})            # not homed yet
        await ctrl.home()
        st = ctrl.status()["axes"]
        assert all(a["homed"] for a in st.values())
        # pan endstop is outside the soft range -> parked inside it
        assert ctrl.position("pan") == pytest.approx(-170, abs=0.1)
        await ctrl.goto({"pan": 30, "tilt": 10, "zoom": 45}, wait=True)
        await asyncio.sleep(0.05)
        pos = ctrl.positions()
        assert pos["pan"] == pytest.approx(30, abs=0.05)
        assert pos["tilt"] == pytest.approx(10, abs=0.05)
        assert pos["zoom"] == pytest.approx(45, abs=0.05)
        # targets are clamped to soft limits
        await ctrl.goto({"tilt": 500}, wait=True)
        await asyncio.sleep(0.05)
        assert ctrl.position("tilt") == pytest.approx(90, abs=0.05)
    run(t, tmp_path)


def test_jog_stops_when_source_goes_silent(tmp_path):
    async def t(ctrl):
        await ctrl.home(["zoom"])
        ctrl.jog({"zoom": 0.5})
        await asyncio.sleep(0.15)
        assert ctrl.state["zoom"].vel > 0
        await asyncio.sleep(1.2)                    # > jog_timeout + ease out
        assert ctrl.state["zoom"].vel == 0
    run(t, tmp_path)


def test_soft_limit_during_jog(tmp_path):
    async def t(ctrl):
        await ctrl.home(["zoom"])
        for _ in range(30):                          # 100 deg/s for 1.5 s
            ctrl.jog({"zoom": 1.0})
            await asyncio.sleep(0.05)
        assert ctrl.position("zoom") <= 90.5
        assert ctrl.status()["axes"]["zoom"]["at_limit"]
    run(t, tmp_path)


def test_estop_blocks_motion(tmp_path):
    async def t(ctrl):
        await ctrl.home(["zoom"])
        await ctrl.estop()
        await asyncio.sleep(0.05)
        assert ctrl.status()["estop"]
        r = await dispatch(ctrl, {"cmd": "goto", "zoom": 10})
        assert not r["ok"]
        await ctrl.clear_estop()
        await asyncio.sleep(0.05)
        assert (await dispatch(ctrl, {"cmd": "goto", "zoom": 10}))["ok"]
    run(t, tmp_path)


def test_presets_via_commands(tmp_path):
    async def t(ctrl):
        await ctrl.home(["zoom"])
        await ctrl.goto({"zoom": 20}, wait=True)
        await asyncio.sleep(0.05)
        assert (await dispatch(ctrl, {"cmd": "preset_save", "preset": 3}))["ok"]
        await ctrl.goto({"zoom": 60}, wait=True)
        await ctrl.recall_preset(3, wait=True)
        await asyncio.sleep(0.05)
        assert ctrl.position("zoom") == pytest.approx(20, abs=0.05)
        data = json.loads((tmp_path / "presets.json").read_text())
        assert "3" in data
    run(t, tmp_path)


def test_visca_drive_and_inquiry(tmp_path):
    async def t(ctrl):
        await ctrl.home(["pan", "tilt"])
        v = ViscaServer(ctrl, "127.0.0.1", 0)
        # pan right, full speed; tilt stop
        r = v._handle(bytes([0x81, 0x01, 0x06, 0x01, 0x18, 0x14, 0x02, 0x03, 0xFF]))
        assert r[0] == bytes([0x90, 0x41, 0xFF])
        assert v.drive["pan"] == pytest.approx(1.0)
        v._handle(bytes([0x81, 0x01, 0x06, 0x01, 0x18, 0x14, 0x03, 0x03, 0xFF]))
        assert v.drive["pan"] == 0
        reply = v._handle(bytes([0x81, 0x09, 0x06, 0x12, 0xFF]))[0]
        assert reply[:2] == bytes([0x90, 0x50]) and len(reply) == 11
    run(t, tmp_path)


def test_home_all_skips_axes_excluded_from_homing(tmp_path):
    text = FAST_CFG.replace("[axis zoom]\n", "[axis zoom]\nhome_with_all: False\n")

    async def t(ctrl):
        await ctrl.home()
        st = ctrl.status()["axes"]
        assert st["pan"]["homed"] and st["tilt"]["homed"]
        assert not st["zoom"]["homed"]
        await ctrl.home(["zoom"])                  # still homable on its own
        assert ctrl.status()["axes"]["zoom"]["homed"]
    run(t, tmp_path, text)


def test_homing_with_switch_inside_range(tmp_path):
    """Original Klipper layout: pan switch at -96 inside -180..180. The axis
    may start on the pressed side; homing must back off until released."""
    text = (FAST_CFG.replace("position_min: -170", "position_min: -180")
            .replace("position_max: 170", "position_max: 180")
            .replace("position_endstop: -175", "position_endstop: -96"))

    async def t(ctrl):
        assert not ctrl.axis("pan").endstop_guard
        await ctrl.home(["pan"])
        assert ctrl.position("pan") == pytest.approx(-96, abs=0.1)
        # move well into the "pressed" region (no guard, so this is allowed)
        await ctrl.goto({"pan": -150}, wait=True)
        await asyncio.sleep(0.05)
        assert ctrl.status()["axes"]["pan"]["endstop"]
        await ctrl.home(["pan"])
        await asyncio.sleep(0.05)
        assert ctrl.position("pan") == pytest.approx(-96, abs=0.1)
    run(t, tmp_path, text)


def test_estop_reset_clears_error_and_diag(tmp_path):
    async def t(ctrl):
        await ctrl.estop()
        ctrl.last_error = "emergency stop"
        await asyncio.sleep(0.05)
        assert ctrl.status()["estop"]
        assert (await dispatch(ctrl, {"cmd": "clear_estop"}))["ok"]
        st = ctrl.status()
        assert not st["estop"] and st["error"] == ""
        r = await dispatch(ctrl, {"cmd": "diag"})
        assert r["ok"] and set(r["diag"]) == {"pan", "tilt", "zoom"}
    run(t, tmp_path)


def test_zero_jog_does_not_cancel_preset_move(tmp_path):
    """A client sending neutral stick values (or a drifting gamepad inside
    the deadband) must not kill a running preset move; a real jog must."""
    async def t(ctrl):
        await ctrl.home(["pan"])
        await ctrl.goto({"pan": 0}, wait=True)
        await ctrl.goto({"pan": 60})
        for _ in range(5):
            ctrl.jog({"pan": 0.0, "tilt": 0.0, "zoom": 0.0})
            await asyncio.sleep(0.02)
        assert ctrl.status()["motion"] == "move"
        while ctrl.status()["motion"]:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        assert ctrl.position("pan") == pytest.approx(60, abs=0.05)
        await ctrl.goto({"pan": -60})
        await asyncio.sleep(0.05)
        ctrl.jog({"pan": 0.5})                       # manual override
        assert ctrl.status()["motion"] == ""
    run(t, tmp_path)


def test_motion_settings_in_units_and_advanced_mode(tmp_path):
    async def t(ctrl):
        # simple mode: one value for all axes, capped per axis
        r = await dispatch(ctrl, {"cmd": "set_motion", "speed": 150, "accel": 900,
                                  "smoothing": 0.8})
        assert r["ok"]
        assert ctrl.speed_of("pan") == 150 and ctrl.accel_of("tilt") == 900
        # advanced mode starts from what was in effect, then per axis
        r = await dispatch(ctrl, {"cmd": "set_motion", "advanced": True,
                                  "axis_speed": {"zoom": 20}, "axis_accel": {"pan": 99999}})
        assert r["ok"] and ctrl.advanced
        assert ctrl.speed_of("zoom") == 20 and ctrl.speed_of("pan") == 150
        assert ctrl.accel_of("pan") == ctrl.axis("pan").max_accel         # capped
        st = ctrl.status()["settings"]
        assert st["effective_speed"]["zoom"] == 20 and st["advanced"] is True
        data = json.loads((tmp_path / "settings.json").read_text())
        assert data["advanced"] and data["speed_axis"]["zoom"] == 20
        await dispatch(ctrl, {"cmd": "set_motion", "advanced": False})
        assert ctrl.speed_of("zoom") == 150
    run(t, tmp_path)


def test_blocking_mode(tmp_path):
    async def t(ctrl):
        await ctrl.home(["pan"])
        assert (await dispatch(ctrl, {"cmd": "lock"}, source="A", client="a"))["ok"]
        assert ctrl.status()["lock"]["owner"] == "a"
        # someone else: control rejected, stop / estop / status still allowed
        r = await dispatch(ctrl, {"cmd": "goto", "pan": 10}, client="b")
        assert not r["ok"] and "locked" in r["error"]
        assert not (await dispatch(ctrl, {"cmd": "goto", "pan": 10}))["ok"]   # UDP / REST
        assert not (await dispatch(ctrl, {"cmd": "lock"}, client="b"))["ok"]
        assert not (await dispatch(ctrl, {"cmd": "unlock"}, client="b"))["ok"]
        assert (await dispatch(ctrl, {"cmd": "stop"}, client="b"))["ok"]
        assert (await dispatch(ctrl, {"cmd": "status"}, client="b"))["ok"]
        ctrl.jog({"pan": 1.0}) if ctrl.may_control(None) else None
        assert ctrl.state["pan"].jog_active is False
        # owner works, then releases
        assert (await dispatch(ctrl, {"cmd": "goto", "pan": 10}, client="a"))["ok"]
        assert (await dispatch(ctrl, {"cmd": "unlock"}, client="a"))["ok"]
        assert (await dispatch(ctrl, {"cmd": "goto", "pan": 0}, client="b"))["ok"]
        # forced unlock only from the Pi itself
        await dispatch(ctrl, {"cmd": "lock"}, client="a")
        assert not (await dispatch(ctrl, {"cmd": "unlock", "force": True}, client="b"))["ok"]             or ctrl.lock_owner == "a"
        assert (await dispatch(ctrl, {"cmd": "unlock", "force": True}, local=True))["ok"]
        assert ctrl.lock_owner is None
    run(t, tmp_path)


def test_record_starts_on_movement_and_trims_end(tmp_path):
    async def t(ctrl):
        await ctrl.home(["pan"])
        await ctrl.goto({"pan": 0}, wait=True)
        await asyncio.sleep(0.1)
        ctrl.record_start("continuous", "armed", on_move=True)
        assert ctrl.recorder.state()["armed"]
        await asyncio.sleep(1.0)                        # idle: not recorded
        assert ctrl.recorder.state()["armed"]
        await ctrl.goto({"pan": 30}, wait=True)
        assert not ctrl.recorder.state()["armed"]
        await asyncio.sleep(1.5)                        # idle end: trimmed
        rec = ctrl.recorder.load(ctrl.record_stop()["name"])
        pts = rec["points"]
        assert pts[0]["pos"]["pan"] == pytest.approx(0, abs=0.05)
        assert pts[-1]["pos"]["pan"] == pytest.approx(30, abs=0.05)
        # ends within one sample of the end of the motion
        moving = [p for p in pts if abs(p["pos"]["pan"] - 30) > 0.05]
        assert pts[-1]["t"] - moving[-1]["t"] <= 0.2
        # nothing moved at all: nothing saved
        ctrl.record_start("continuous", on_move=True)
        await asyncio.sleep(0.2)
        with pytest.raises(Exception, match="never moved"):
            ctrl.record_stop()
    run(t, tmp_path)


def test_upload_validation(tmp_path):
    async def t(ctrl):
        good = {"name": "up", "mode": "keypoints",
                "points": [{"t": 5, "pos": {"pan": 0}}, {"t": 7, "pos": {"pan": 20}}]}
        s = ctrl.import_recording(good)
        assert s["name"] == "up" and s["duration"] == 2.0
        assert ctrl.import_recording(good)["name"] == "up (2)"     # never overwrites
        bad = [
            {"points": [{"t": 0, "pos": {"pan": 0}}]},                          # 1 point
            {"points": [{"t": 0, "pos": {"pan": 0}}, {"t": 0, "pos": {"pan": 1}}]},  # time
            {"points": [{"t": 0, "pos": {"foo": 0}}, {"t": 1, "pos": {"foo": 1}}]},  # axis
            {"points": [{"t": 0, "pos": {"pan": 0}}, {"t": 1, "pos": {"pan": 999}}]},  # limit
            {"points": [{"t": 0, "pos": {"pan": 0}}, {"t": 1, "pos": {"tilt": 1}}]},  # axes
            {"points": "nope"}, [1, 2],
        ]
        for b in bad:
            with pytest.raises(Exception):
                ctrl.import_recording(b)
    run(t, tmp_path)


def test_record_continuous_and_replay(tmp_path):
    async def t(ctrl):
        await ctrl.home(["pan", "tilt"])
        await ctrl.goto({"pan": 0, "tilt": 0}, wait=True)
        assert (await dispatch(ctrl, {"cmd": "record_start", "name": "sweep"}))["ok"]
        await ctrl.goto({"pan": 40, "tilt": 20}, wait=True)
        await ctrl.goto({"pan": -20, "tilt": 10}, wait=True)
        r = await dispatch(ctrl, {"cmd": "record_stop"})
        assert r["ok"] and r["recording"]["name"] == "sweep"
        assert r["recording"]["points"] > 10
        duration = r["recording"]["duration"]
        await ctrl.goto({"pan": 100, "tilt": -30}, wait=True)
        # replay at 2x: goes back to the start, plays, ends where recording ended
        loop = asyncio.get_running_loop()
        assert (await dispatch(ctrl, {"cmd": "play", "name": "sweep", "speed": 2.0}))["ok"]
        t0 = None
        while ctrl.playback is not None:
            if t0 is None and ctrl.playback["phase"] == "playing":
                t0 = loop.time()
            await asyncio.sleep(0.02)
        played = loop.time() - t0
        assert played < duration * 0.75
        await asyncio.sleep(0.3)
        assert ctrl.position("pan") == pytest.approx(-20, abs=0.1)
        assert ctrl.position("tilt") == pytest.approx(10, abs=0.1)
    run(t, tmp_path)


def test_record_keypoints_loop_and_stop(tmp_path):
    async def t(ctrl):
        await ctrl.home(["pan"])
        await ctrl.goto({"pan": 0}, wait=True)
        ctrl.record_start("keypoints", "kp")
        with pytest.raises(Exception):
            ctrl.record_start("keypoints")             # already recording
        await ctrl.goto({"pan": 30}, wait=True)
        assert ctrl.record_keypoint() == 2
        await ctrl.goto({"pan": 10}, wait=True)
        assert ctrl.record_keypoint() == 3
        summary = ctrl.record_stop()
        assert summary["points"] == 3 and summary["mode"] == "keypoints"
        await ctrl.play("kp", speed=4.0, loop=True)
        passes = 0
        for _ in range(400):
            await asyncio.sleep(0.02)
            passes = ctrl.playback["pass"]
            if passes >= 2:
                break
        assert passes >= 2                               # looped
        assert (await dispatch(ctrl, {"cmd": "play_stop"}))["ok"]
        assert ctrl.playback is None
        lst = (await dispatch(ctrl, {"cmd": "recordings"}))["recordings"]
        assert [r["name"] for r in lst] == ["kp"]
        assert (await dispatch(ctrl, {"cmd": "recording_delete", "name": "kp"}))["ok"]
    run(t, tmp_path)


def test_move_onto_end_of_travel_switch_completes(tmp_path):
    """Zoom-like axis: switch at position_min. Going to the limit lands on the
    switch; the MCU guard halts it there and the move must still complete."""
    text = FAST_CFG.replace("[axis zoom]\n", "[axis zoom]\nendstop_pin: ^gpio25\n"
                            "homing_speed: 200\nsecond_homing_speed: 50\n")

    async def t(ctrl):
        assert ctrl.axis("zoom").endstop_guard
        await ctrl.home(["zoom"])
        await ctrl.goto({"zoom": 30}, wait=True)
        await asyncio.wait_for(ctrl.goto({"zoom": 0}, wait=True), 5)
        assert ctrl.position("zoom") == pytest.approx(0, abs=0.2)
    run(t, tmp_path, text)
