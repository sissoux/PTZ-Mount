"""Klipper-like INI configuration loader.

Everything a user may want to tune lives in config/ptz.cfg. This module turns
it into typed dataclasses. Unknown options are reported so typos are caught.
"""
from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .protocol import MAX_AXES, PIN_NONE

#: Max step rate per axis of the firmware (STEP_TICK_HZ / 2 in firmware/src/config.h)
MCU_MAX_STEP_RATE = 20000.0


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- pins
@dataclass(frozen=True)
class Pin:
    gpio: int = PIN_NONE
    invert: bool = False
    pullup: bool = False

    @property
    def used(self) -> bool:
        return self.gpio != PIN_NONE


_PIN_RE = re.compile(r"^([\^~!]*)gpio(\d+)$", re.IGNORECASE)


def parse_pin(text: Optional[str]) -> Pin:
    if text is None or text.strip() == "":
        return Pin()
    m = _PIN_RE.match(text.strip())
    if not m:
        raise ConfigError(f"invalid pin '{text}' (expected e.g. '!gpio12', '^gpio4')")
    mods, num = m.group(1), int(m.group(2))
    if not 0 <= num <= 29:
        raise ConfigError(f"RP2040 has no gpio{num}")
    return Pin(num, invert="!" in mods, pullup="^" in mods)


def parse_ratio(text: Optional[str]) -> float:
    """Klipper gear_ratio syntax: '80:16, 3:1' -> 15.0"""
    if not text:
        return 1.0
    ratio = 1.0
    for part in text.split(","):
        a, b = part.split(":")
        ratio *= float(a) / float(b)
    return ratio


# ---------------------------------------------------------------- sections
class _Section:
    """Typed getters that remember which options were consumed."""

    def __init__(self, cp: configparser.ConfigParser, name: str):
        self.name = name
        self._sec = cp[name] if cp.has_section(name) else {}
        self._used = set()

    def _raw(self, key, default):
        self._used.add(key)
        if key in self._sec and self._sec[key].strip() != "":
            return self._sec[key].strip()
        if default is _REQUIRED:
            raise ConfigError(f"[{self.name}] missing option '{key}'")
        return default

    def get(self, key, default=None):
        return self._raw(key, default)

    def getint(self, key, default=None):
        v = self._raw(key, default)
        return None if v is None else int(v)

    def getfloat(self, key, default=None, minval=None, maxval=None):
        v = self._raw(key, default)
        if v is None:
            return None
        v = float(v)
        if minval is not None and v < minval:
            raise ConfigError(f"[{self.name}] {key} must be >= {minval}")
        if maxval is not None and v > maxval:
            raise ConfigError(f"[{self.name}] {key} must be <= {maxval}")
        return v

    def getbool(self, key, default=None):
        v = self._raw(key, default)
        if isinstance(v, bool) or v is None:
            return v
        return str(v).lower() in ("1", "true", "yes", "on")

    def unused(self) -> List[str]:
        return [k for k in self._sec.keys() if k not in self._used]


_REQUIRED = object()


# ---------------------------------------------------------------- dataclasses
@dataclass
class McuConfig:
    serial: str
    baud: int
    status_rate: int
    watchdog_timeout: float


@dataclass
class ServerConfig:
    http_host: str
    http_port: int
    udp_port: int
    visca_port: int
    status_rate: float
    state_dir: str


@dataclass
class MotionConfig:
    deadband: float
    expo: float
    jog_timeout: float
    enable_on_start: bool
    home_on_start: List[str]
    accel: float = 1.0          # default acceleration factor (web UI slider)
    smoothing: float = 0.3      # default ease in/out amount 0..1
    ease_time: float = 0.6      # s of acceleration ramp at smoothing = 1
    stream_rate: float = 100.0  # Hz, host -> MCU velocity stream


@dataclass
class TmcUartConfig:
    rx_pin: Pin
    tx_pin: Pin
    baud: int


@dataclass
class TmcConfig:
    uart_address: int
    run_current: float
    hold_current: float
    sense_resistor: float
    interpolate: bool
    stealthchop_threshold: float


@dataclass
class AxisConfig:
    name: str
    index: int
    step_pin: Pin
    dir_pin: Pin
    enable_pin: Pin
    endstop_pin: Pin
    microsteps: int
    full_steps_per_rotation: int
    gear_ratio: float
    rotation_distance: float
    position_min: float
    position_max: float
    position_endstop: float
    homing_positive_dir: bool
    homing_speed: float
    second_homing_speed: float
    homing_retract_dist: float
    park_position: Optional[float]
    max_velocity: float
    max_accel: float
    jog_velocity: float
    jog_accel: float
    home_with_all: bool = True       # included in "Home all" / default homing
    endstop_guard: bool = True       # MCU halts the axis on its endstop outside homing
    tmc: Optional[TmcConfig] = None

    @property
    def steps_per_unit(self) -> float:
        return (self.full_steps_per_rotation * self.microsteps * self.gear_ratio
                / self.rotation_distance)

    def to_steps(self, units: float) -> int:
        return int(round(units * self.steps_per_unit))

    def to_units(self, steps: float) -> float:
        return steps / self.steps_per_unit

    @property
    def has_endstop(self) -> bool:
        return self.endstop_pin.used


@dataclass
class PtzConfig:
    path: str
    mcu: McuConfig
    server: ServerConfig
    motion: MotionConfig
    tmc_uart: Optional[TmcUartConfig]
    axes: Dict[str, AxisConfig] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    disabled_axes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- loader
def load(path: str) -> PtzConfig:
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"),
                                   interpolation=None)
    cp.optionxform = str  # keep case
    with open(path, encoding="utf-8") as f:
        cp.read_file(f)
    sections: List[_Section] = []

    def sec(name):
        s = _Section(cp, name)
        sections.append(s)
        return s

    s = sec("mcu")
    mcu = McuConfig(serial=s.get("serial", "/dev/serial0"),
                    baud=s.getint("baud", 500000),
                    status_rate=s.getint("status_rate", 50),
                    watchdog_timeout=s.getfloat("watchdog_timeout", 0.3, 0.05, 60))

    s = sec("server")
    server = ServerConfig(http_host=s.get("http_host", "0.0.0.0"),
                          http_port=s.getint("http_port", 8080),
                          udp_port=s.getint("udp_port", 9000),
                          visca_port=s.getint("visca_port", 52381),
                          status_rate=s.getfloat("status_rate", 20, 1, 200),
                          state_dir=os.path.expanduser(s.get("state_dir", "~/.ptz")))

    s = sec("motion")
    home = [h.strip() for h in (s.get("home_on_start", "") or "").split(",") if h.strip()]
    motion = MotionConfig(deadband=s.getfloat("deadband", 0.04, 0, 0.5),
                          expo=s.getfloat("expo", 0.5, 0, 1),
                          jog_timeout=s.getfloat("jog_timeout", 0.5, 0.05, 10),
                          enable_on_start=s.getbool("enable_on_start", True),
                          home_on_start=home,
                          accel=s.getfloat("accel", 1.0, 0.02, 1.0),
                          smoothing=s.getfloat("smoothing", 0.3, 0.0, 1.0),
                          ease_time=s.getfloat("ease_time", 0.6, 0.05, 3.0),
                          stream_rate=s.getfloat("stream_rate", 100.0, 20.0, 250.0))

    tmc_uart = None
    if cp.has_section("tmc_uart"):
        s = sec("tmc_uart")
        tmc_uart = TmcUartConfig(rx_pin=parse_pin(s.get("uart_pin", _REQUIRED)),
                                 tx_pin=parse_pin(s.get("tx_pin", _REQUIRED)),
                                 baud=s.getint("baud", 115200))

    axes: Dict[str, AxisConfig] = {}
    disabled: List[str] = []
    for name in cp.sections():
        if not name.startswith("axis "):
            continue
        aname = name.split(None, 1)[1].strip()
        if not _Section(cp, name).getbool("enabled", True):
            disabled.append(aname)       # axis ignored entirely (hardware not fitted)
            continue
        if len(axes) >= MAX_AXES:
            raise ConfigError(f"too many axes (max {MAX_AXES})")
        s = sec(name)
        s.get("enabled")
        pmin = s.getfloat("position_min", _REQUIRED)
        pmax = s.getfloat("position_max", _REQUIRED)
        if pmax <= pmin:
            raise ConfigError(f"[{name}] position_max must be > position_min")
        max_vel = s.getfloat("max_velocity", _REQUIRED, 0)
        max_acc = s.getfloat("max_accel", _REQUIRED, 0)
        homing_speed = s.getfloat("homing_speed", max_vel / 4, 0)
        park = s.getfloat("park_position", None)
        ax = AxisConfig(
            name=aname, index=len(axes),
            step_pin=parse_pin(s.get("step_pin", _REQUIRED)),
            dir_pin=parse_pin(s.get("dir_pin", _REQUIRED)),
            enable_pin=parse_pin(s.get("enable_pin", "")),
            endstop_pin=parse_pin(s.get("endstop_pin", "")),
            microsteps=s.getint("microsteps", 16),
            full_steps_per_rotation=s.getint("full_steps_per_rotation", 200),
            gear_ratio=parse_ratio(s.get("gear_ratio", "")),
            rotation_distance=s.getfloat("rotation_distance", _REQUIRED, 0),
            position_min=pmin, position_max=pmax,
            position_endstop=s.getfloat("position_endstop", pmin),
            homing_positive_dir=s.getbool("homing_positive_dir", False),
            homing_speed=homing_speed,
            second_homing_speed=s.getfloat("second_homing_speed", homing_speed / 2, 0),
            homing_retract_dist=s.getfloat("homing_retract_dist", 5, 0),
            park_position=park,
            max_velocity=max_vel, max_accel=max_acc,
            jog_velocity=s.getfloat("jog_velocity", max_vel, 0, max_vel),
            jog_accel=s.getfloat("jog_accel", max_acc, 0, max_acc),
            home_with_all=s.getbool("home_with_all", True),
        )
        # Endstop guard: only meaningful when the switch sits at the end of
        # travel. If position_endstop is inside [min, max] (switch in the middle
        # of the range, as on the original Klipper setup), the switch may be
        # pressed during normal moves, so the guard is off by default.
        guard = (s.get("endstop_guard", "auto") or "auto").lower()
        if guard == "auto":
            if ax.homing_positive_dir:
                ax.endstop_guard = ax.position_endstop >= pmax
            else:
                ax.endstop_guard = ax.position_endstop <= pmin
        else:
            ax.endstop_guard = guard in ("1", "true", "yes", "on")
        if max_vel * ax.steps_per_unit > MCU_MAX_STEP_RATE:
            raise ConfigError(
                f"[{name}] max_velocity {max_vel} needs {max_vel * ax.steps_per_unit:.0f} "
                f"steps/s, firmware limit is {MCU_MAX_STEP_RATE:.0f} "
                f"(max {MCU_MAX_STEP_RATE / ax.steps_per_unit:.1f} units/s)")
        if ax.homing_speed > max_vel or ax.second_homing_speed > max_vel:
            raise ConfigError(f"[{name}] homing speeds must be <= max_velocity")
        if ax.microsteps not in (1, 2, 4, 8, 16, 32, 64, 128, 256):
            raise ConfigError(f"[{name}] invalid microsteps {ax.microsteps}")
        if park is not None and not pmin <= park <= pmax:
            raise ConfigError(f"[{name}] park_position outside limits")
        tname = f"tmc2209 {aname}"
        if cp.has_section(tname):
            t = sec(tname)
            # Klipper puts the bus pins in every [tmc2209] section: accept them
            rx, tx = t.get("uart_pin", None), t.get("tx_pin", None)
            if rx:
                bus = TmcUartConfig(parse_pin(rx), parse_pin(tx) if tx else parse_pin(rx), 115200)
                if tmc_uart is None:
                    tmc_uart = bus
                elif (bus.rx_pin.gpio, bus.tx_pin.gpio) != (tmc_uart.rx_pin.gpio, tmc_uart.tx_pin.gpio):
                    raise ConfigError(f"[{tname}] uart_pin/tx_pin differ from the shared TMC bus")
            run = t.getfloat("run_current", _REQUIRED, 0.05, 2.0)
            ax.tmc = TmcConfig(
                uart_address=t.getint("uart_address", 0),
                run_current=run,
                hold_current=t.getfloat("hold_current", run, 0, 2.0),
                sense_resistor=t.getfloat("sense_resistor", 0.110, 0.01),
                interpolate=t.getbool("interpolate", True),
                stealthchop_threshold=t.getfloat("stealthchop_threshold", 0, 0),
            )
        axes[aname] = ax

    known = {"mcu", "server", "motion", "tmc_uart"}
    cfg = PtzConfig(path=path, mcu=mcu, server=server, motion=motion,
                    tmc_uart=tmc_uart, axes=axes)
    for name in cp.sections():
        if name not in known and not name.startswith(("axis ", "tmc2209 ")):
            cfg.warnings.append(f"unknown section [{name}]")
        tm = name.split(None, 1)[1] if name.startswith("tmc2209 ") else None
        if tm and tm not in axes and tm not in disabled:
            cfg.warnings.append(f"[{name}] has no matching [axis ...]")
    for s in sections:
        for k in s.unused():
            cfg.warnings.append(f"[{s.name}] unknown option '{k}'")
    cfg.tmc_uart = tmc_uart
    for h in list(motion.home_on_start):
        if h in disabled:
            motion.home_on_start.remove(h)
            cfg.warnings.append(f"home_on_start: axis '{h}' is disabled, skipped")
        elif h not in axes:
            raise ConfigError(f"home_on_start: unknown axis '{h}'")
    cfg.disabled_axes = disabled
    return cfg
