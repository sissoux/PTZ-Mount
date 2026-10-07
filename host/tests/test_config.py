import pytest

from conftest import CONFIG_PATH
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
    pan = cfg.axes["pan"]
    assert pan.index == 0
    assert pan.steps_per_unit == pytest.approx(200 * 16 * 5 / 360)
    assert pan.enable_pin.invert and pan.endstop_pin.pullup
    assert pan.tmc.uart_address == 0
    assert cfg.tmc_uart.tx_pin.gpio == 8


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
    regs = dict(tmc2209.register_values(pan))
    assert (regs[tmc2209.CHOPCONF] >> 24) & 0xF == 4          # 16 microsteps
    assert regs[tmc2209.GCONF] & (1 << 6)                      # pdn_disable
    assert not regs[tmc2209.GCONF] & (1 << 2)                  # stealthChop
    assert regs[tmc2209.IHOLD_IRUN] >> 8 & 0x1F == irun
