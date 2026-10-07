import pytest

from conftest import CONFIG_PATH, PREVIOUS_CONFIG_PATH
from ptz import config as C
from ptz import tmc2209


def test_parse_pin():
    assert C.parse_pin("gpio11") == C.Pin(11, False, False)
    assert C.parse_pin("!gpio12") == C.Pin(12, True, False)
    assert C.parse_pin("^!gpio4") == C.Pin(4, True, True)
    assert not C.parse_pin("").used
    with pytest.raises(C.ConfigError):
        C.parse_pin("PA1")
    with pytest.raises(C.ConfigError):
        C.parse_pin("gpio30")


def test_parse_ratio():
    assert C.parse_ratio("80:16") == 5.0
    assert C.parse_ratio("80:16, 3:1") == 15.0
    assert C.parse_ratio("") == 1.0


def test_load_default_config():
    cfg = C.load(CONFIG_PATH)
    assert cfg.warnings == []
    assert list(cfg.axes) == ["pan", "tilt", "zoom"]
    assert cfg.disabled_axes == ["focus"]
    pan = cfg.axes["pan"]
    assert pan.index == 0
    assert pan.steps_per_unit == pytest.approx(400 * 16 * (144 / 17) / 360)
    assert pan.enable_pin.invert and pan.endstop_pin.pullup and pan.endstop_pin.invert
    assert pan.tmc.uart_address == 0
    assert cfg.tmc_uart.tx_pin.gpio == 8
    # switches inside the travel range -> no guard; zoom switch at min -> guard
    assert not pan.endstop_guard and not cfg.axes["tilt"].endstop_guard
    assert cfg.axes["zoom"].endstop_guard


# Klipper section -> PTZ axis
KLIPPER_MAP = {"stepper_x": "pan", "stepper_y": "tilt", "stepper_z": "zoom",
               "extruder": "focus"}


def test_compatible_with_previous_klipper_config():
    """Hardware facts of config/PreviousConfig.cfg must be preserved."""
    import configparser
    old = configparser.ConfigParser(inline_comment_prefixes=("#", ";"),
                                    interpolation=None, strict=False)
    old.read(PREVIOUS_CONFIG_PATH, encoding="utf-8")
    new = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
    new.read(CONFIG_PATH, encoding="utf-8")
    cfg = C.load(CONFIG_PATH)
    for ksec, axis in KLIPPER_MAP.items():
        k, n = old[ksec], new[f"axis {axis}"]
        for key in ("step_pin", "dir_pin", "enable_pin", "endstop_pin", "microsteps"):
            if key in k:
                if key == "microsteps":
                    assert int(k[key]) == int(n[key]), (axis, key)
                else:
                    assert C.parse_pin(k[key]) == C.parse_pin(n[key]), (axis, key)
        if axis == "focus":      # extruder: only pins matter
            continue
        a = cfg.axes[axis]
        assert a.full_steps_per_rotation == int(k.get("full_steps_per_rotation", 200)), axis
        assert a.gear_ratio == pytest.approx(C.parse_ratio(k.get("gear_ratio", ""))), axis
        assert a.rotation_distance == float(k["rotation_distance"]), axis
        assert a.position_endstop == float(k["position_endstop"]), axis
        assert a.position_max == float(k["position_max"]), axis
        assert a.position_min == float(k.get("position_min", 0)), axis
        assert a.homing_positive_dir == (k.get("homing_positive_dir", "False") == "True"), axis
        t_old = old[f"tmc2209 {ksec}"]
        assert a.tmc.uart_address == int(t_old["uart_address"]), axis
        assert a.tmc.run_current == float(t_old["run_current"]), axis
        if "hold_current" in t_old:
            assert a.tmc.hold_current == float(t_old["hold_current"]), axis
        assert C.parse_pin(t_old["uart_pin"]) == cfg.tmc_uart.rx_pin
        assert C.parse_pin(t_old["tx_pin"]) == cfg.tmc_uart.tx_pin
    assert cfg.mcu.serial == old["mcu"]["serial"]


def _axis_cfg(extra=""):
    return ("[axis zoom]\nstep_pin: gpio19\ndir_pin: gpio28\nposition_min: 0\n"
            "position_max: 90\nrotation_distance: 360\nmax_velocity: 10\nmax_accel: 10\n"
            + extra)


def test_disabled_axis_and_home_with_all(tmp_path):
    p = tmp_path / "x.cfg"
    p.write_text(_axis_cfg("home_with_all: False\n"))
    assert C.load(str(p)).axes["zoom"].home_with_all is False
    p.write_text("[motion]\nhome_on_start: zoom\n" + _axis_cfg("enabled: False\n")
                 + "[tmc2209 zoom]\nrun_current: 0.5\n")
    cfg = C.load(str(p))
    assert cfg.axes == {} and cfg.disabled_axes == ["zoom"]
    assert cfg.motion.home_on_start == []
    assert all("unknown" not in w and "no matching" not in w for w in cfg.warnings)


def test_step_rate_limit(tmp_path):
    p = tmp_path / "x.cfg"
    p.write_text(_axis_cfg().replace("max_velocity: 10", "max_velocity: 5000"))
    with pytest.raises(C.ConfigError, match="steps/s"):
        C.load(str(p))


def test_klipper_style_tmc_bus_pins(tmp_path):
    p = tmp_path / "x.cfg"
    p.write_text(_axis_cfg() + "[tmc2209 zoom]\nuart_pin: gpio9\ntx_pin: gpio8\nrun_current: 0.5\n")
    cfg = C.load(str(p))
    assert (cfg.tmc_uart.rx_pin.gpio, cfg.tmc_uart.tx_pin.gpio) == (9, 8)


def test_unknown_option_is_reported(tmp_path):
    p = tmp_path / "x.cfg"
    p.write_text("[mcu]\nserail: /dev/ttyAMA0\n"
                 "[axis pan]\nstep_pin: gpio1\ndir_pin: gpio2\nposition_min: 0\n"
                 "position_max: 10\nrotation_distance: 360\nmax_velocity: 10\nmax_accel: 10\n")
    cfg = C.load(str(p))
    assert any("serail" in w for w in cfg.warnings)


def test_tmc_registers():
    cfg = C.load(CONFIG_PATH)
    pan = cfg.axes["pan"]
    vsense, irun, ihold = tmc2209.compute_currents(0.8, 0.5, 0.110)
    assert vsense and irun == 25
    vsense, irun, ihold = tmc2209.compute_currents(pan.tmc.run_current,
                                                   pan.tmc.hold_current, 0.110)
    regs = dict(tmc2209.register_values(pan))
    assert (regs[tmc2209.CHOPCONF] >> 24) & 0xF == 4          # 16 microsteps
    assert regs[tmc2209.GCONF] & (1 << 6)                      # pdn_disable
    assert not regs[tmc2209.GCONF] & (1 << 2)                  # stealthChop
    assert regs[tmc2209.IHOLD_IRUN] >> 8 & 0x1F == irun
