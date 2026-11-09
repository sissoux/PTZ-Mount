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
        # parked at 0 after homing (default park_position)
        assert ctrl.position("pan") == pytest.approx(0, abs=0.1)
        assert ctrl.position("tilt") == pytest.approx(0, abs=0.1)
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
            .replace("position_endstop: -175", "position_endstop: -96\npark_position: none"))

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


def test_unhomed_axis_cannot_move(tmp_path):
    async def t(ctrl):
        # jog refused before homing, from any source
        r = await dispatch(ctrl, {"cmd": "jog", "pan": 1.0, "tilt": 0.0})
        assert not r["ok"] and "pan not homed" in r["error"]
        assert ctrl.jog({"tilt": -1.0}) == ["tilt"]
        await asyncio.sleep(0.3)
        assert ctrl.state["pan"].vel == 0 and ctrl.state["tilt"].vel == 0
        assert not (await dispatch(ctrl, {"cmd": "goto", "pan": 10}))["ok"]
        # homing is always allowed, then the axis moves
        await ctrl.home(["pan"])
        assert ctrl.jog({"pan": 0.5, "tilt": 0.5}) == ["tilt"]
        await asyncio.sleep(0.2)
        assert ctrl.state["pan"].vel != 0 and ctrl.state["tilt"].vel == 0
        # motors off: homing lost, motion refused again
        await ctrl.enable(False, ["pan"])
        await ctrl.enable(True, ["pan"])
        assert ctrl.jog({"pan": 0.5}) == ["pan"]
    run(t, tmp_path)


def test_require_homing_can_be_disabled(tmp_path):
    text = FAST_CFG.replace("[motion]\n", "[motion]\nrequire_homing: False\n")

    async def t(ctrl):
        assert ctrl.jog({"tilt": 0.5}) == []
        await asyncio.sleep(0.2)
        assert ctrl.state["tilt"].vel != 0
    run(t, tmp_path, text)


def test_race_learning_and_tracking(tmp_path, monkeypatch):
    import ptz.track
    monkeypatch.setattr(ptz.track, "MIN_LAP_S", 0.2)     # simulated laps are short

    async def t(ctrl):
        await ctrl.home(["pan", "tilt"])
        await ctrl.goto({"pan": 0, "tilt": 0}, wait=True)
        assert (await dispatch(ctrl, {"cmd": "record_start", "mode": "laps",
                                      "name": "Circuit"}))["ok"]
        # 3 laps: start line at pan 0, the car goes to pan 40 and comes back
        ctrl.lap_mark()
        for lap in range(3):
            await ctrl.goto({"pan": 40, "tilt": 10}, wait=True)
            await ctrl.goto({"pan": 0, "tilt": 0}, wait=True)
            ctrl.lap_mark()
        r = await dispatch(ctrl, {"cmd": "record_stop"})
        assert r["ok"], r
        track = r["recording"]
        assert track["mode"] == "track" and track["name"] == "Circuit"
        assert len(track["laps"]) == 3 and track["lap_time"] > 0.3
        names = {x["name"]: x["mode"] for x in r["recordings"]}
        assert names == {"Circuit": "track", "Circuit (raw)": "laps-raw"}
        # rebuild without lap 2
        r = await dispatch(ctrl, {"cmd": "track_build", "source": "Circuit (raw)",
                                  "exclude": [2]})
        assert [s["used"] for s in r["recording"]["laps"]] == [True, False, True]

        # tracking: arm, GO from the "timing system" even while a page holds the lock
        await ctrl.goto({"pan": 20}, wait=True)
        assert (await dispatch(ctrl, {"cmd": "track_arm", "name": "Circuit"}))["ok"]
        for _ in range(200):
            if ctrl.tracking["phase"] == "armed":
                break
            await asyncio.sleep(0.02)
        assert ctrl.tracking["phase"] == "armed"
        assert ctrl.position("pan") == pytest.approx(0, abs=0.1)
        await dispatch(ctrl, {"cmd": "lock"}, client="operator")
        assert (await dispatch(ctrl, {"cmd": "track_go"}, source="udp timing"))["ok"]
        assert ctrl.tracking["phase"] == "running"
        await dispatch(ctrl, {"cmd": "unlock"}, client="operator")
        peak = 0.0
        for _ in range(500):
            peak = max(peak, ctrl.position("pan"))
            if ctrl.tracking["runs"] == 1 and ctrl.tracking["phase"] == "armed":
                break
            await asyncio.sleep(0.01)
        assert peak > 30                       # it really followed the lap
        assert ctrl.tracking["runs"] == 1 and ctrl.tracking["phase"] == "armed"  # re-armed
        # target lap time sets the speed factor
        ctrl.track_set(target_lap=track["lap_time"] / 2)
        assert ctrl.status()["tracking"]["speed_eff"] == pytest.approx(2.0, rel=0.05)
        assert (await dispatch(ctrl, {"cmd": "track_abort"}))["ok"]
        assert ctrl.tracking["phase"] == "idle"
        with pytest.raises(Exception):
            ctrl.track_go()
    run(t, tmp_path)


def test_go_during_lap_restarts_smoothly_and_auto_adjust(tmp_path, monkeypatch):
    import ptz.track
    monkeypatch.setattr(ptz.track, "MIN_LAP_S", 0.2)

    async def t(ctrl):
        await ctrl.home(["pan"])
        await ctrl.goto({"pan": 0}, wait=True)
        ctrl.record_start("laps", "Ring")
        ctrl.lap_mark()
        for _ in range(2):
            await ctrl.goto({"pan": 60}, speed=0.3, wait=True)
            await ctrl.goto({"pan": 0}, speed=0.3, wait=True)
            ctrl.lap_mark()
        lap_time = ctrl.record_stop()["lap_time"]
        assert lap_time > 1.0
        await ctrl.track_arm("Ring")
        while ctrl.tracking["phase"] != "armed":
            await asyncio.sleep(0.02)
        ctrl.track_go()
        await asyncio.sleep(lap_time * 0.4)                 # mid-lap, far from the start
        assert ctrl.position("pan") > 20
        # second GO mid-lap: lap restarts from the start, gliding (no jump)
        loop = asyncio.get_running_loop()
        prev = None
        ctrl.track_go()
        assert ctrl.tracking["phase"] == "running"
        speeds = []
        for _ in range(int(lap_time / 0.02)):
            await asyncio.sleep(0.02)
            p, now = ctrl._current_state(["pan"])[0]["pan"], loop.time()
            if prev is not None and now > prev[1]:
                speeds.append(abs(p - prev[0]) / (now - prev[1]))
            prev = (p, now)
        assert ctrl.tracking["runs"] >= 1
        # the set point never moves faster than the axis can: no jump to the start
        assert max(speeds) <= ctrl.axis("pan").max_velocity * 1.15
        await ctrl.track_abort()

        # auto adjust: halfway to the measured lap, ignore outliers
        ctrl.tracking["lap_time"] = 18.0
        ctrl.track_set(target_lap=0, speed=1.0, auto_adjust=True, adjust_tolerance=0.3)
        ctrl._auto_adjust(17.0)
        assert ctrl.current_lap_time() == pytest.approx(17.5)
        ctrl._auto_adjust(40.0)                               # crash / artefact
        assert ctrl.current_lap_time() == pytest.approx(17.5)
        assert "ignored" in ctrl.tracking["adjust_note"]
        ctrl._auto_adjust(3.0)                                # GO from another car
        assert ctrl.current_lap_time() == pytest.approx(17.5)
    run(t, tmp_path)


def test_jog_scale_applies_after_deadband(tmp_path):
    """A controller speed slider at 5 % must still move the axis slowly
    (scaling before the deadband would zero it)."""
    async def t(ctrl):
        await ctrl.home(["pan"])
        await ctrl.goto({"pan": 0}, wait=True)
        r = await dispatch(ctrl, {"cmd": "jog", "pan": 0.8, "scale": {"pan": 0.05}})
        assert r["ok"]
        st = ctrl.state["pan"]
        full = ctrl._shape(0.8)
        assert st.jog_value == pytest.approx(full * 0.05)
        assert st.jog_value > 0                                     # slow, but moving
        await ctrl.stop()
        ctrl.jog({"pan": 0.8}, scale=1.0)
        assert ctrl.state["pan"].jog_value == pytest.approx(full)
        ctrl.jog({"pan": 0.8}, scale="junk")                         # ignored, no crash
        await ctrl.stop()
    run(t, tmp_path)


def test_inactive_axis_is_left_out_and_can_be_switched_on(tmp_path):
    text = FAST_CFG.replace("[axis zoom]\n", "[axis zoom]\nactive: False\n")

    async def t(ctrl):
        assert ctrl.status()["axes"]["zoom"]["active"] is False
        await asyncio.sleep(0.1)
        assert not ctrl.status()["axes"]["zoom"]["enabled"]           # driver unpowered
        await ctrl.home()                                             # Home all skips it
        st = ctrl.status()["axes"]
        assert st["pan"]["homed"] and st["tilt"]["homed"] and not st["zoom"]["homed"]
        assert ctrl.position("pan") == pytest.approx(0, abs=0.1)     # parked at 0
        with pytest.raises(Exception, match="disabled"):
            await ctrl.home(["zoom"])
        assert ctrl.jog({"zoom": 1.0}) == []                          # silently ignored
        assert not ctrl.state["zoom"].jog_active
        r = await dispatch(ctrl, {"cmd": "goto", "zoom": 10})
        assert not r["ok"] and "disabled" in r["error"]
        await ctrl.goto({"pan": 20, "zoom": 50}, wait=True)           # zoom left out
        assert ctrl.position("pan") == pytest.approx(20, abs=0.1)
        # switch it on live: powered, must be homed, then usable
        assert (await dispatch(ctrl, {"cmd": "axis_active", "axis": "zoom", "on": True}))["ok"]
        await asyncio.sleep(0.1)
        assert ctrl.status()["axes"]["zoom"]["enabled"] and not ctrl.state["zoom"].homed
        await ctrl.home(["zoom"])
        await ctrl.goto({"zoom": 30}, wait=True)
        assert ctrl.position("zoom") == pytest.approx(30, abs=0.1)
        data = json.loads((tmp_path / "settings.json").read_text())
        assert data["axis_active"]["zoom"] is True                    # remembered
        await ctrl.set_axis_active("zoom", False)
        await asyncio.sleep(0.1)
        assert not ctrl.status()["axes"]["zoom"]["enabled"]
    run(t, tmp_path, text)


def test_wait_option_replies_when_motion_is_finished(tmp_path):
    async def t(ctrl):
        r = await dispatch(ctrl, {"cmd": "home", "axes": ["pan"], "wait": True})
        assert r["ok"] and ctrl.state["pan"].homed and not ctrl._homing
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        r = await dispatch(ctrl, {"cmd": "goto", "pan": 120, "wait": True})
        assert r["ok"] and loop.time() - t0 > 0.2                       # waited for the move
        assert ctrl.position("pan") == pytest.approx(120, abs=0.1)
        ctrl.save_preset(4)
        await ctrl.goto({"pan": 0}, wait=True)
        r = await dispatch(ctrl, {"cmd": "preset_recall", "preset": 4, "wait": True})
        assert r["ok"] and ctrl.position("pan") == pytest.approx(120, abs=0.1)
        # interrupted while waiting -> error reply
        task = asyncio.create_task(dispatch(ctrl, {"cmd": "goto", "pan": -150, "wait": True}))
        await asyncio.sleep(0.1)
        await dispatch(ctrl, {"cmd": "stop"})
        r = await task
        assert not r["ok"] and "interrupted" in r["error"]
    run(t, tmp_path)


def test_learned_soft_limits(tmp_path):
    async def t(ctrl):
        await ctrl.home(["pan", "tilt"])
        # learn: jog/move to the boundary, then "set"
        await ctrl.goto({"tilt": 20}, wait=True)
        await asyncio.sleep(0.05)
        r = await dispatch(ctrl, {"cmd": "limit_set", "axis": "tilt", "side": "max"})
        assert r["ok"] and r["value"] == pytest.approx(20, abs=0.1)
        await ctrl.goto({"tilt": -10}, wait=True)
        await asyncio.sleep(0.05)
        await dispatch(ctrl, {"cmd": "limit_set", "axis": "tilt", "side": "min"})
        bad = await dispatch(ctrl, {"cmd": "limit_set", "axis": "tilt", "side": "min", "value": 30})
        assert not bad["ok"]                                    # min above max refused
        # not enabled yet: free travel
        await ctrl.goto({"tilt": 40}, wait=True)
        assert ctrl.position("tilt") == pytest.approx(40, abs=0.1)
        assert (await dispatch(ctrl, {"cmd": "limits_enable", "on": True}))["ok"]
        assert ctrl.status()["limits"]["effective"]["tilt"] == pytest.approx([-10, 20], abs=0.1)
        # outside when enabled: may only come back
        await ctrl.goto({"tilt": 0}, wait=True)
        await ctrl.goto({"tilt": 60}, wait=True)                # clamped
        await asyncio.sleep(0.05)
        assert ctrl.position("tilt") == pytest.approx(20, abs=0.1)
        for _ in range(40):                                     # jog hard upwards
            ctrl.jog({"tilt": 1.0})
            await asyncio.sleep(0.03)
        assert ctrl.position("tilt") <= 20.5
        # a replay going beyond the limit is held inside
        pts = [{"t": 0, "pos": {"tilt": 0.0}}, {"t": 0.6, "pos": {"tilt": 60.0}},
               {"t": 1.2, "pos": {"tilt": 0.0}}]
        ctrl.import_recording({"name": "over", "points": pts})
        await ctrl.play("over")
        peak = -99
        while ctrl.playback is not None:
            peak = max(peak, ctrl.position("tilt"))
            await asyncio.sleep(0.02)
        assert peak <= 20.6
        # persisted; disabling gives the full travel back
        data = json.loads((tmp_path / "settings.json").read_text())
        assert data["user_limits"]["enabled"] and data["user_limits"]["axes"]["tilt"]["max"] == pytest.approx(20, abs=0.1)
        await dispatch(ctrl, {"cmd": "limits_enable", "on": False})
        await ctrl.goto({"tilt": 50}, wait=True)
        assert ctrl.position("tilt") == pytest.approx(50, abs=0.1)
        await dispatch(ctrl, {"cmd": "limit_clear", "axis": "tilt"})
        assert ctrl.user_limits["tilt"] == {"min": None, "max": None}
    run(t, tmp_path)


def test_editor_save_replaces_with_backup_and_hides_scratch(tmp_path):
    import os

    async def t(ctrl):
        pts = [{"t": 0, "pos": {"pan": 0.0, "tilt": 0.0}}, {"t": 2, "pos": {"pan": 10.0, "tilt": 5.0}}]
        ctrl.import_recording({"name": "Spa", "mode": "track", "lap_time": 2.0, "points": pts})
        edited = {"mode": "track", "laps": [{"lap": 1}], "source": "Spa (raw)",
                  "points": [{"t": 0, "pos": {"pan": 1.0, "tilt": 0.0}},
                             {"t": 3, "pos": {"pan": 12.0, "tilt": 4.0}}]}
        s = ctrl.save_edited_recording("Spa", edited)
        assert s["name"] == "Spa" and s["lap_time"] == 3 and s["source"] == "Spa (raw)"
        assert ctrl.recorder.load("Spa")["points"][1]["pos"]["pan"] == 12.0
        backups = os.listdir(tmp_path / "recordings" / "backups")
        assert len(backups) == 1 and backups[0].startswith("Spa-")
        ctrl.save_edited_recording("~preview", edited)
        assert [r["name"] for r in ctrl.recorder.list()] == ["Spa"]      # scratch hidden
        assert ctrl.recorder.load("~preview")["mode"] == "track"
        with pytest.raises(Exception):                                    # same checks as upload
            ctrl.save_edited_recording("Spa", {"points": [{"t": 0, "pos": {"pan": 999}},
                                                          {"t": 1, "pos": {"pan": 0}}]})
    run(t, tmp_path)


def test_replaced_move_does_not_leave_other_axis_running(tmp_path):
    """A new move on tilt only, while pan is moving: pan must come to a stop,
    not keep its last streamed speed on the board (it used to run on until
    the tilt move ended and the board watchdog fired)."""
    async def t(ctrl):
        await ctrl.home(["pan", "tilt"])
        await ctrl.goto({"pan": 150})
        await asyncio.sleep(0.6)
        assert abs(ctrl.state["pan"].vel) > 0
        at_cancel = ctrl.position("pan")
        await ctrl.goto({"tilt": 85}, speed=0.1)
        await asyncio.sleep(0.8)
        assert ctrl.status()["motion"] == "move"          # tilt still moving
        assert ctrl.state["pan"].vel == 0
        stopped_at = ctrl.position("pan")
        assert stopped_at < at_cancel + 6                 # braked, not run to the 170 end
        await asyncio.sleep(0.3)
        assert ctrl.position("pan") == pytest.approx(stopped_at, abs=0.01)
    run(t, tmp_path)


def test_homing_one_axis_while_another_parks(tmp_path):
    """Home pan, then tilt while pan is still moving to its park position:
    both end parked and neither homing fails with 'move interrupted'."""
    async def t(ctrl):
        first = asyncio.create_task(ctrl.home(["pan"]))
        while not (ctrl.state["pan"].homed and ctrl.status()["motion"]):
            await asyncio.sleep(0.02)
        await ctrl.home(["tilt"])
        await first                                       # no exception
        await asyncio.sleep(0.1)
        assert ctrl.position("pan") == pytest.approx(0, abs=0.1)
        assert ctrl.position("tilt") == pytest.approx(0, abs=0.1)
        assert ctrl.last_error == ""
    run(t, tmp_path)


def test_shutdown_rests_head_turns_motors_off_and_powers_off(tmp_path):
    import sys

    async def t(ctrl):
        await ctrl.home(["pan", "tilt"])
        await ctrl.goto({"pan": 30, "tilt": 20}, wait=True)
        # pan 0, tilt at its max end (here the learned soft limit)
        await ctrl.set_user_limit("tilt", "max", 60)
        await ctrl.enable_user_limits(True)
        assert ctrl.shutdown_targets() == {"pan": 0.0, "tilt": 60.0}
        ctrl.POWEROFF_CMD = (sys.executable, "-c", "pass")      # stands for systemctl poweroff
        r = await dispatch(ctrl, {"cmd": "shutdown"})
        assert r["ok"] and r["targets"] == {"pan": 0.0, "tilt": 60.0}
        assert not (await dispatch(ctrl, {"cmd": "shutdown"}))["ok"]   # only once
        while ctrl.shutdown_state["phase"] not in ("powering_off", "safe", "failed"):
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        assert ctrl.shutdown_state["phase"] == "powering_off"
        assert ctrl.position("pan") == pytest.approx(0, abs=0.1)
        assert ctrl.position("tilt") == pytest.approx(60, abs=0.1)
        st = ctrl.status()
        assert not st["axes"]["pan"]["enabled"] and not st["axes"]["tilt"]["enabled"]
        assert not st["axes"]["pan"]["homed"]
        assert st["shutdown"]["phase"] == "powering_off"
    run(t, tmp_path)


def test_shutdown_without_poweroff_permission_is_safe_to_unplug(tmp_path):
    import sys

    async def t(ctrl):
        await ctrl.home(["pan", "tilt"])
        ctrl.POWEROFF_CMD = (sys.executable, "-c", "import sys; print('sudo: a password is required'); sys.exit(1)")
        await ctrl.shutdown()
        assert ctrl.shutdown_state["phase"] == "safe"
        assert "password is required" in ctrl.shutdown_state["message"]
        assert ctrl.position("tilt") == pytest.approx(90, abs=0.1)     # configured maximum
        await ctrl.shutdown(poweroff=False)       # a new request after "safe" is refused
    with pytest.raises(MotionError):
        run(t, tmp_path)


def test_settings_file_written_atomically(tmp_path):
    async def t(ctrl):
        await ctrl.enable_user_limits(True)
        data = json.loads((tmp_path / "settings.json").read_text())
        assert data["user_limits"]["enabled"] is True
        assert not (tmp_path / "settings.json.tmp").exists()
    run(t, tmp_path)


def test_base_levelling_keeps_pan_jog_flat(tmp_path):
    async def t(ctrl):
        await ctrl.home(["pan", "tilt"])
        await ctrl.goto({"pan": -40, "tilt": 5}, wait=True)
        await asyncio.sleep(0.05)
        # learn the base: camera "level" at two pans (simulated base pitch 4, roll -3)
        from ptz.level import Level
        truth = Level(4.0, -3.0)
        for p in (-40, 50):
            await ctrl.goto({"pan": p, "tilt": truth.to_mount(p, 0.0)[1]}, wait=True)
            await asyncio.sleep(0.05)
            r = await dispatch(ctrl, {"cmd": "level_mark"})
            assert r["ok"]
        lv = ctrl.status()["level"]
        assert lv["pitch"] == pytest.approx(4.0, abs=0.1) and lv["roll"] == pytest.approx(-3.0, abs=0.1)
        assert (await dispatch(ctrl, {"cmd": "level_enable", "on": True}))["ok"]
        await ctrl.goto({"pan": -60, "tilt": truth.to_mount(-60, 10.0)[1]}, wait=True)
        await asyncio.sleep(0.05)
        e0 = ctrl.status()["level"]["world"]["tilt"]
        tilts, elev = [], []
        for _ in range(40):                                  # pan only, up to the limit
            ctrl.jog({"pan": 1.0})
            await asyncio.sleep(0.03)
            tilts.append(ctrl.position("tilt"))
            elev.append(ctrl.status()["level"]["world"]["tilt"])
        await asyncio.sleep(0.8)
        assert ctrl.position("pan") > 40
        assert max(tilts) - min(tilts) > 4                   # the motor tilt did follow
        assert max(abs(e - e0) for e in elev) < 0.2          # the aim stayed level
        # pan ran into its end of travel: tilt stopped with it, still level
        assert ctrl.status()["level"]["world"]["tilt"] == pytest.approx(e0, abs=0.2)
        assert ctrl.state["tilt"].vel == 0 and ctrl.state["pan"].vel == 0
        # off: plain mount jog, tilt untouched
        await dispatch(ctrl, {"cmd": "level_enable", "on": False})
        t0 = ctrl.position("tilt")
        for _ in range(10):
            ctrl.jog({"pan": -1.0})
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.6)
        assert ctrl.position("tilt") == pytest.approx(t0, abs=0.01)
        data = json.loads((tmp_path / "settings.json").read_text())
        assert data["level"]["pitch"] == pytest.approx(4.0, abs=0.1)
        assert not (await dispatch(ctrl, {"cmd": "level_set", "pitch": 60}))["ok"]
        await dispatch(ctrl, {"cmd": "level_clear"})
        assert ctrl.status()["level"]["marks"] == [] and ctrl.level_pitch == 0
    run(t, tmp_path)


def test_several_cars_learning_and_tracking(tmp_path, monkeypatch):
    import ptz.track
    monkeypatch.setattr(ptz.track, "MIN_LAP_S", 0.2)     # simulated laps are short

    async def lap_of(ctrl):
        await ctrl.goto({"pan": 40, "tilt": 10}, wait=True)
        await ctrl.goto({"pan": 0, "tilt": 0}, wait=True)

    async def t(ctrl):
        await ctrl.home(["pan", "tilt"])
        await ctrl.goto({"pan": 0, "tilt": 0}, wait=True)
        assert (await dispatch(ctrl, {"cmd": "record_start", "mode": "laps", "name": "Multi"}))["ok"]
        # the timing software follows car 1
        assert (await dispatch(ctrl, {"cmd": "await_car", "car": 1}))["state"] == "learning"
        assert (await dispatch(ctrl, {"cmd": "lap", "car": 1}))["action"] == "mark"
        await lap_of(ctrl)
        assert (await dispatch(ctrl, {"cmd": "lap", "car": 7}))["action"] == "none"   # other car
        assert (await dispatch(ctrl, {"cmd": "lap", "car": 1}))["action"] == "mark"
        # half way round, switch to car 2: the head goes back to the line and waits
        await ctrl.goto({"pan": 30, "tilt": 8}, wait=True)
        await dispatch(ctrl, {"cmd": "await_car", "car": 2})
        assert ctrl.status()["recording"]["waiting"] and ctrl.status()["cars"]["waiting"]
        for _ in range(200):
            if ctrl._traj is None and abs(ctrl.position("pan")) < 0.2:
                break
            await asyncio.sleep(0.02)
        assert ctrl.position("pan") == pytest.approx(0, abs=0.2)
        assert (await dispatch(ctrl, {"cmd": "lap", "car": 1}))["action"] == "none"
        assert (await dispatch(ctrl, {"cmd": "lap", "car": 2}))["action"] == "mark"
        assert not ctrl.status()["recording"]["waiting"]
        await lap_of(ctrl)
        await dispatch(ctrl, {"cmd": "lap", "car": 2})
        assert ctrl.status()["recording"]["laps"] == 2
        r = await dispatch(ctrl, {"cmd": "record_stop"})
        assert r["ok"], r
        assert len(r["recording"]["laps"]) == 2         # the broken lap is not used
        raw = ctrl.recorder.load("Multi (raw)")
        assert len(raw["breaks"]) == 1 and len(raw["markers"]) == 4

        # tracking car 2, then the operator switches to car 3 (page locked)
        assert (await dispatch(ctrl, {"cmd": "track_arm", "name": "Multi", "wait": True}))["ok"]
        await dispatch(ctrl, {"cmd": "lock"}, client="operator")
        await asyncio.sleep(0.3)                          # car 2's next crossing
        assert (await dispatch(ctrl, {"cmd": "lap", "car": 2}, source="udp timing"))["action"] == "go"
        assert ctrl.tracking["phase"] == "running"
        await asyncio.sleep(0.3)
        r = await dispatch(ctrl, {"cmd": "await_car", "car": 3}, source="udp timing")
        assert r["ok"] and r["state"] == "tracking" and ctrl.current_car == 3
        for _ in range(200):
            if ctrl.tracking["phase"] == "armed":
                break
            await asyncio.sleep(0.02)
        assert ctrl.tracking["phase"] == "armed"
        assert ctrl.position("pan") == pytest.approx(0, abs=0.2)
        assert (await dispatch(ctrl, {"cmd": "lap", "car": 2}, source="udp timing"))["action"] == "none"
        assert ctrl.tracking["phase"] == "armed"
        assert (await dispatch(ctrl, {"cmd": "lap", "car": 3}, source="udp timing"))["action"] == "go"
        assert ctrl.tracking["phase"] == "running"
        assert (await dispatch(ctrl, {"cmd": "lap", "car": 3}))["action"] == "ignored"  # repeat
        await dispatch(ctrl, {"cmd": "unlock"}, client="operator")
        cars = {c["car"]: c for c in ctrl.status()["cars"]["list"]}
        assert cars[1]["laps"] == 2 and cars[1]["best_lap"] > 0.3 and cars[2]["laps"] >= 1
        assert (await dispatch(ctrl, {"cmd": "car_name", "car": 3, "name": "Dupont"}))["ok"]
        saved = json.loads((tmp_path / "settings.json").read_text())["cars"]
        assert {"car": 3, "name": "Dupont"} in saved and [c["car"] for c in saved] == [1, 7, 2, 3]
        assert not (await dispatch(ctrl, {"cmd": "await_car", "car": 0}))["ok"]
        await dispatch(ctrl, {"cmd": "cars_reset"})
        assert all(c["laps"] == 0 for c in ctrl.status()["cars"]["list"])
        await ctrl.track_abort()
    run(t, tmp_path)


def test_car_list_and_follow_leader(tmp_path, monkeypatch):
    import ptz.track
    monkeypatch.setattr(ptz.track, "MIN_LAP_S", 0.2)

    async def lap_of(ctrl):
        await ctrl.goto({"pan": 40, "tilt": 10}, wait=True)
        await ctrl.goto({"pan": 0, "tilt": 0}, wait=True)

    async def lap(ctrl, car, **kw):
        r = await dispatch(ctrl, {"cmd": "lap", "car": car, **kw}, source="udp timing")
        assert r["ok"], r
        return r

    async def t(ctrl):
        await ctrl.home(["pan", "tilt"])
        await ctrl.goto({"pan": 0, "tilt": 0}, wait=True)
        r = await dispatch(ctrl, {"cmd": "cars_set", "cars": [44, {"car": 7, "name": "Dupont"}, 3, 44]})
        assert r["ok"] and r["cars"] == [44, 7, 3]
        st = (await dispatch(ctrl, {"cmd": "cars"}))["cars"]
        assert [c["car"] for c in st["list"]] == [44, 7, 3] and st["list"][1]["name"] == "Dupont"

        # learning, following the leader: 7 leads, then 44 overtakes
        await dispatch(ctrl, {"cmd": "record_start", "mode": "laps", "name": "Lead"})
        r = await dispatch(ctrl, {"cmd": "follow_leader", "on": True})
        assert r["ok"] and r["leader"] is None and ctrl.follow_leader
        r = await lap(ctrl, 7)
        assert r["action"] == "mark" and r.get("switched") and ctrl.current_car == 7
        await asyncio.sleep(0.25)
        assert (await lap(ctrl, 44))["action"] == "none"      # behind 7
        await lap_of(ctrl)
        assert (await lap(ctrl, 7))["action"] == "mark"
        await asyncio.sleep(0.25)
        assert (await lap(ctrl, 44))["action"] == "none"
        await ctrl.goto({"pan": 20, "tilt": 5}, wait=True)   # part of 7's lap
        r = await lap(ctrl, 44)                               # 44 crosses first: new leader
        assert r["action"] == "mark" and r.get("switched") and ctrl.current_car == 44
        assert ctrl.status()["cars"]["leader"] == 44 and len(ctrl.recorder.breaks) == 1
        await ctrl.goto({"pan": 0, "tilt": 0}, wait=True)
        await lap_of(ctrl)
        assert (await lap(ctrl, 7))["action"] == "none"
        assert (await lap(ctrl, 44))["action"] == "mark"
        r = await dispatch(ctrl, {"cmd": "record_stop"})
        assert r["ok"], r
        assert len(r["recording"]["laps"]) == 2               # the overtaken lap is dropped

        # tracking: new race, the first to cross leads; then positions from the software
        assert (await dispatch(ctrl, {"cmd": "track_arm", "name": "Lead", "wait": True}))["ok"]
        await dispatch(ctrl, {"cmd": "lock"}, client="operator")
        assert (await dispatch(ctrl, {"cmd": "cars_reset"}, source="udp timing"))["ok"]
        await asyncio.sleep(0.3)
        r = await lap(ctrl, 3)
        assert r["action"] == "go" and r.get("switched") and ctrl.current_car == 3
        assert (await lap(ctrl, 7))["action"] == "none"
        await asyncio.sleep(0.3)
        r = await lap(ctrl, 44, position=1)
        assert r["action"] == "go" and ctrl.current_car == 44
        assert (await lap(ctrl, 3, position=2))["action"] == "none"
        assert ctrl.status()["cars"]["leader"] == 44
        # choosing a car by hand turns it off
        r = await dispatch(ctrl, {"cmd": "await_car", "car": 7}, source="udp timing")
        assert r["ok"] and not ctrl.follow_leader and ctrl.current_car == 7
        await dispatch(ctrl, {"cmd": "unlock"}, client="operator")
        await ctrl.track_abort()

        # a full list refuses new cars; flush forgets everything
        assert (await dispatch(ctrl, {"cmd": "cars_set", "cars": list(range(1, 13))}))["ok"]
        assert ctrl.current_car == 7
        assert (await lap(ctrl, 99))["action"] == "unknown"
        assert not (await dispatch(ctrl, {"cmd": "await_car", "car": 99}))["ok"]
        assert not (await dispatch(ctrl, {"cmd": "cars_set", "cars": list(range(1, 14))}))["ok"]
        assert (await dispatch(ctrl, {"cmd": "cars_flush"}))["ok"]
        st = ctrl.status()["cars"]
        assert st["list"] == [] and st["current"] is None
        assert json.loads((tmp_path / "settings.json").read_text())["cars"] == []
    run(t, tmp_path)
