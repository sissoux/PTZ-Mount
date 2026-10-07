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
        await asyncio.sleep(0.8)                    # > jog_timeout, no refresh
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
