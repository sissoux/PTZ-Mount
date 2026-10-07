"""TMC2209 register computation.

The firmware only knows how to write/read a raw register over the TMC UART
bus. All the "intelligence" (current scaling, microstep resolution, chopper
settings) lives here so it can be edited without reflashing the RP2040.
Formulas follow the TMC2209 datasheet (and match Klipper's tmc2209.py).
"""
from __future__ import annotations

import math
from typing import List, Tuple

from .config import AxisConfig

# Registers
GCONF = 0x00
GSTAT = 0x01
IFCNT = 0x02
IHOLD_IRUN = 0x10
TPOWERDOWN = 0x11
TSTEP = 0x12
TPWMTHRS = 0x13
CHOPCONF = 0x6C
DRV_STATUS = 0x6F
PWMCONF = 0x70
IOIN = 0x06

TMC_CLOCK = 12_000_000
MAX_CURRENT = 2.0


def _mres(microsteps: int) -> int:
    return 8 - int(math.log2(microsteps))


def current_bits(current: float, sense_resistor: float, vsense: bool) -> int:
    vref = 0.180 if vsense else 0.325
    cs = int(32.0 * (sense_resistor + 0.020) * current * math.sqrt(2.0) / vref + 0.5) - 1
    return max(0, min(31, cs))


def compute_currents(run: float, hold: float, rsense: float) -> Tuple[bool, int, int]:
    """Return (vsense, irun, ihold). Uses vsense=1 when it gives better resolution."""
    run = min(run, MAX_CURRENT)
    vsense = current_bits(run, rsense, False) < 16
    irun = current_bits(run, rsense, vsense)
    ihold = current_bits(min(hold, run), rsense, vsense)
    return vsense, irun, ihold


def tpwmthrs(axis: AxisConfig, velocity: float) -> int:
    """stealthChop -> spreadCycle switch velocity (units/s) to TPWMTHRS value."""
    if velocity <= 0:
        return 0xFFFFF
    step_dist = 1.0 / axis.steps_per_unit           # units per microstep
    step_rate = velocity / step_dist                 # microsteps per second
    tstep = TMC_CLOCK / (step_rate * (256 / axis.microsteps))
    return max(0, min(0xFFFFF, int(tstep)))


def register_values(axis: AxisConfig) -> List[Tuple[int, int]]:
    """List of (register, value) to write, in order."""
    t = axis.tmc
    if t is None:
        return []
    vsense, irun, ihold = compute_currents(t.run_current, t.hold_current,
                                           t.sense_resistor)
    # Very high threshold => stealthChop at all speeds (Klipper semantics)
    always_stealth = t.stealthchop_threshold >= axis.max_velocity
    spreadcycle_only = t.stealthchop_threshold <= 0

    gconf = (1 << 6)          # pdn_disable: UART controls current
    gconf |= (1 << 7)         # mstep_reg_select: MRES from CHOPCONF
    gconf |= (1 << 8)         # multistep_filt
    if spreadcycle_only:
        gconf |= (1 << 2)     # en_spreadcycle

    chopconf = 3              # toff
    chopconf |= 5 << 4        # hstrt
    chopconf |= 0 << 7        # hend
    chopconf |= 2 << 15       # tbl
    chopconf |= int(vsense) << 17
    chopconf |= _mres(axis.microsteps) << 24
    chopconf |= int(t.interpolate) << 28

    ihold_irun = ihold | (irun << 8) | (8 << 16)   # iholddelay = 8

    if always_stealth or spreadcycle_only:
        thrs = 0
    else:
        thrs = tpwmthrs(axis, t.stealthchop_threshold)

    return [
        (GCONF, gconf),
        (CHOPCONF, chopconf),
        (IHOLD_IRUN, ihold_irun),
        (TPOWERDOWN, 20),
        (TPWMTHRS, thrs),
    ]


def decode_ioin(value: int) -> dict:
    """Pin levels as seen by the driver itself (TMC2209 IOIN register)."""
    return {
        "enn": bool(value & (1 << 0)), "ms1": bool(value & (1 << 2)),
        "ms2": bool(value & (1 << 3)), "diag": bool(value & (1 << 4)),
        "pdn_uart": bool(value & (1 << 6)), "step": bool(value & (1 << 7)),
        "spread_en": bool(value & (1 << 8)), "dir": bool(value & (1 << 9)),
        "version": (value >> 24) & 0xFF,
    }


def decode_drv_status(value: int) -> dict:
    """Human readable DRV_STATUS (diagnostics for the web UI / logs)."""
    return {
        "otpw": bool(value & (1 << 0)), "ot": bool(value & (1 << 1)),
        "s2ga": bool(value & (1 << 2)), "s2gb": bool(value & (1 << 3)),
        "ola": bool(value & (1 << 6)), "olb": bool(value & (1 << 7)),
        "cs_actual": (value >> 16) & 0x1F,
        "stealth": bool(value & (1 << 30)), "standstill": bool(value & (1 << 31)),
    }
