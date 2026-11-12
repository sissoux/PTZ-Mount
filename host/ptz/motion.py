"""Motion controller: the brain of the PTZ head, running on the Raspberry Pi.

Responsibilities
  * configure the MCU (pins, limits, TMC2209 registers) from ptz.cfg
  * convert user units <-> microsteps
  * motion shaping, streamed to the MCU as velocities at ~100 Hz:
      - jog: joystick values -> deadband/expo -> accel + ease in/out shaper
      - goto / presets: synchronized multi-axis S-curve moves
      - playback of recorded movements, at a variable speed, once or in loop
  * homing sequence (fast approach, retract, slow approach, set position)
  * presets, recordings, enable/disable, stop / emergency stop
  * aggregate status for the API layer

The RP2040 does the hard real-time part (step pulses, endstop halt, hard
acceleration cap, soft limits, watchdog). A late host tick only means a
velocity is held a few ms longer; every move ends with an exact MOVE_TO.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Optional, Set, Tuple

from . import protocol as P
from . import tmc2209
from .config import AxisConfig, PtzConfig
from .fsutil import atomic_write_json, sync_all
from .level import Level, solve as level_solve, spread as level_spread
from .mcu import McuError, McuLink
from .presets import PresetStore
from .recorder import Recorder
from . import track as _track
from .trajectory import (BlendIn, MoveTrajectory, PlaybackTrajectory, VelocityShaper,
                         braking_speed, clamp)

log = logging.getLogger("ptz.motion")


class MotionError(Exception):
    pass


@dataclass
class AxisState:
    pos: int = 0              # microsteps
    vel: float = 0.0          # microsteps/s
    flags: int = 0
    homed: bool = False
    jog_value: float = 0.0    # shaped, -1..1
    jog_time: float = 0.0
    jog_active: bool = False
    braking: bool = False     # jog currently limited by the soft-limit approach


KP = 2.0                      # position feedback gain while streaming (1/s)
FEEDBACK_LATENCY = 0.004      # s, host receive time vs MCU position sample
LOCAL_ADDRESSES = ("127.0.0.1", "::1", "localhost")
SOURCE_TTL = 10.0             # s, how long a UDP/VISCA sender is listed as active
CARS = 12                     # cars in the list (the car followed is one of them)
CAR_NUMBER_MAX = 999          # race numbers 1..999
LENS_LEARN_TTL = 60.0         # s, lens learning ends unless the wizard renews it
ZOOM_LOCKED = ("zoom locked: no lens selected (the servo could force a lens's ring "
               "past its end). Choose the lens fitted, or learn it")
# pulse range of the servo zoom in settings saved by v0.14.0 (no servo_range)
_V0140_SERVO_RANGE = [0.0, 100.0, 1000.0, 2000.0]
_UNSET = object()


class MotionController:
    def __init__(self, cfg: PtzConfig, link: McuLink):
        self.cfg = cfg
        self.link = link
        self.axes: List[AxisConfig] = sorted(cfg.axes.values(), key=lambda a: a.index)
        self.state: Dict[str, AxisState] = {a.name: AxisState() for a in self.axes}
        self.presets = PresetStore(os.path.join(cfg.server.state_dir, "presets.json"))
        m = cfg.motion
        # Speed / acceleration in axis units. Simple mode: one value for every
        # axis (capped by each axis maximum). Advanced mode: one per axis.
        self.advanced = False
        self.speed_all = max(a.jog_velocity for a in self.axes) if self.axes else 1.0
        self.accel_all = max(a.jog_accel for a in self.axes) if self.axes else 1.0
        self.speed_axis = {a.name: a.jog_velocity for a in self.axes}
        self.accel_axis = {a.name: a.jog_accel for a in self.axes}
        self.smoothing = m.smoothing          # ease in/out 0..1
        # Blocking mode: only the lock owner (a web client id) may move the head
        # axes in use (an absent zoom drive can be switched off live)
        self.axis_active = {a.name: a.active for a in self.axes}
        # learned soft limits (inside the configured travel), on/off from the UI
        self.user_limits: Dict[str, Dict[str, Optional[float]]] = {
            a.name: {"min": None, "max": None} for a in self.axes}
        self.user_limits_on = False
        # base levelling: jog pan / tilt in a level frame (mount at an angle)
        self.level_on = False
        self.level_pitch = 0.0
        self.level_roll = 0.0
        self.level_marks: List[Tuple[float, float]] = []    # (pan, tilt) aimed level
        # zoom lenses: name -> {"focal_min", "focal_max", "wide", "tele",
        # "points", "tilt_max"} (wide / tele: zoom axis position at each end of
        # the ring; points: [position, focal] marks in between; tilt_max: the
        # lowest the camera may tilt with this lens on, None: soft limits)
        self.lenses: Dict[str, dict] = {}
        self.lens: Optional[str] = None             # lens fitted (None: raw motor)
        self.tilt_adjust = False                    # finding the lens's max tilt
        # servo zoom with no lens: locked (no motion), except while a lens is
        # being learned (renewed by the wizard, LENS_LEARN_TTL)
        self._learn_until = 0.0
        self._learning_was = False
        self._zoom_lock_log = 0.0
        self._range_converted = False
        # multi-car race: the car list (in the timing software's order), the
        # car followed, and the lap times of every car seen
        self.cars: Dict[int, dict] = {}
        self.current_car: Optional[int] = None
        self.follow_leader = False                  # follow whoever leads the race
        self._car_cross: Dict[int, float] = {}      # car -> last line crossing (loop time)
        self._car_pos: Dict[int, int] = {}          # car -> race position sent with its lap
        self._car_seq: Dict[int, int] = {}          # car -> order of its last crossing
        self._seq = 0
        self.lock_owner: Optional[str] = None
        self.lock_label = ""
        self.sources: Dict[Tuple[str, str], float] = {}   # (kind, ip) -> last seen
        self._refused_log = 0.0
        self.play_speed = 1.0                 # replay speed factor
        self.play_loop = False
        # servo axes: last position (absolute, set again after a restart so the
        # servo does not jump), firmware without servo output
        self._servo_pos: Dict[str, float] = {}
        self._servo_saved = 0.0
        self.fw_version: Optional[int] = None
        self.servo_unsupported: List[str] = []
        self.mcu_warning = ""
        self._settings_path = os.path.join(cfg.server.state_dir, "settings.json")
        self._load_settings()
        self._level = Level(self.level_pitch, self.level_roll)
        self._level_hold: Optional[float] = None    # level tilt kept during a pan jog
        self.recorder = Recorder(os.path.join(cfg.server.state_dir, "recordings"),
                                 self._homed_positions, time.monotonic)
        self.playback: Optional[dict] = None
        self._play_task: Optional[asyncio.Task] = None
        # race tracking: arm on a learned lap, GO on the timing signal
        self.tracking = {"phase": "idle", "track": None, "lap_time": None, "speed": 1.0,
                         "target_lap": None, "auto_rearm": True, "runs": 0,
                         # auto adjust: next lap time = halfway to the measured one
                         "auto_adjust": False, "adjust_tolerance": 0.3,
                         "last_interval": None, "adjust_note": ""}
        self._last_go: Optional[float] = None
        self._track_task: Optional[asyncio.Task] = None
        self._go: Optional[asyncio.Event] = None
        self._traj = None                     # MoveTrajectory | PlaybackTrajectory
        self._traj_kind = ""
        self.shutdown_state: Optional[dict] = None    # {"phase", "message"} once requested
        self._traj_future: Optional[asyncio.Future] = None
        self._shapers = {a.name: VelocityShaper() for a in self.axes}
        self._cmd_vel = {a.name: 0.0 for a in self.axes}       # last streamed, units/s
        self._des_hist: Deque[Tuple[float, Dict[str, float]]] = deque(maxlen=100)
        self.sys_flags = 0
        self.connected = False
        self.ready = False
        self.last_error = ""
        self._last_status = 0.0
        self._homing: Set[str] = set()
        self._raw: Dict[str, dict] = {}       # raw step moves running (wiring check)
        self._waiters: List[tuple] = []       # (axis_idx, types, future)
        self._tasks: List[asyncio.Task] = []
        link.on_status = self._on_status
        link.on_event = self._on_event
        if self._range_converted:            # servo pulse range changed: store the
            self._save_settings()            # converted positions with the new range

    # ================================================================ lifecycle
    async def start(self) -> None:
        await self.link.start()
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._supervisor()))
        self._tasks.append(loop.create_task(self._stream_loop()))

    async def close(self) -> None:
        self._cancel_motion()
        for t in self._tasks:
            t.cancel()
        try:
            await self.link.request("STOP", axis_mask=0xFF, retries=0)
        except Exception:
            pass
        await self.link.close()

    async def _supervisor(self) -> None:
        """(Re)configure the MCU whenever it is not ready."""
        while True:
            if not self.ready:
                try:
                    await self._configure()
                    if self.cfg.motion.home_on_start:
                        asyncio.get_running_loop().create_task(
                            self._safe(self.home(self.cfg.motion.home_on_start)))
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"MCU configuration failed: {e}"
                    log.error(self.last_error)
                    await asyncio.sleep(2.0)
                    continue
            now = asyncio.get_running_loop().time()
            self.connected = (now - self._last_status) < 1.0
            if self.ready:
                self._servo_persist()
                if self._learning_was and not self.lens_learning:
                    log.warning("lens learning ended (not renewed): zoom locked")
                    await self._safe(self._zoom_relock())
            await asyncio.sleep(0.25)

    async def _safe(self, coro):
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            log.error("%s", e)

    async def _configure(self) -> None:
        c = self.cfg
        pong = await self.link.request("PING", timeout=0.5)
        fw = pong["protocol_version"]
        if not P.MIN_PROTOCOL_VERSION <= fw <= P.PROTOCOL_VERSION:
            raise MotionError(f"firmware protocol v{fw}, host expects "
                              f"v{P.MIN_PROTOCOL_VERSION}..v{P.PROTOCOL_VERSION}: reflash the MCU")
        self.fw_version = fw
        self.servo_unsupported = [a.name for a in self.axes if a.servo and fw < 2]
        self.mcu_warning = ""
        if self.servo_unsupported:
            self.mcu_warning = (f"the board firmware (protocol v{fw}) has no servo output: "
                                f"{', '.join(self.servo_unsupported)} unavailable. Reflash the "
                                "SKR Pico (firmware/flash.sh)")
            log.warning("%s", self.mcu_warning)
            for n in self.servo_unsupported:
                self.axis_active[n] = False
        await self.link.request("RESET")
        await self.link.request("SET_STATUS_RATE", rate_hz=c.mcu.status_rate)
        await self.link.request("SET_WATCHDOG",
                                timeout_ms=int(c.mcu.watchdog_timeout * 1000))
        # Axes first, motors re-energized right away: RESET released the drivers
        # and the tilt must not stay unpowered while the TMC registers are
        # written (the drivers keep their previous register values meanwhile).
        for ax in self.axes:
            if ax.name in self.servo_unsupported:
                self.state[ax.name].homed = False
                continue
            flags = P.AXF_SERVO if ax.servo else 0
            flags |= P.AXF_DIR_INVERT if ax.dir_pin.invert else 0
            flags |= P.AXF_ENABLE_INVERT if ax.enable_pin.invert else 0
            flags |= P.AXF_ENDSTOP_INVERT if ax.endstop_pin.invert else 0
            flags |= P.AXF_ENDSTOP_PULLUP if ax.endstop_pin.pullup else 0
            es_dir = ((1 if ax.homing_positive_dir else -1)
                      if ax.has_endstop and ax.endstop_guard else 0)
            spu = ax.steps_per_unit
            await self.link.request(
                "CONFIG_AXIS", axis=ax.index, step_pin=ax.step_pin.gpio,
                dir_pin=ax.dir_pin.gpio, enable_pin=ax.enable_pin.gpio,
                endstop_pin=ax.endstop_pin.gpio, flags=flags, endstop_dir=es_dir,
                max_vel=ax.max_velocity * spu, max_accel=ax.max_accel * spu,
                # the host shapes jog/moves; the MCU follows up to its hard cap
                vel_accel=ax.max_accel * spu)
            self.state[ax.name].homed = False
            if ax.servo:
                await self._servo_init(ax)
        if c.motion.enable_on_start:
            await self.enable(True)
        if c.tmc_uart:
            await self.link.request("CONFIG_TMC_UART", rx_pin=c.tmc_uart.rx_pin.gpio,
                                    tx_pin=c.tmc_uart.tx_pin.gpio, baud=c.tmc_uart.baud)
            for ax in self.axes:
                await self._configure_tmc(ax)
        self.ready = True
        self.last_error = ""
        log.info("MCU configured: %d axes (%s)", len(self.axes),
                 ", ".join(a.name for a in self.axes))

    async def _configure_tmc(self, ax: AxisConfig) -> None:
        if ax.tmc is None:
            return
        addr = ax.tmc.uart_address
        try:
            before = (await self.link.request("TMC_READ", addr=addr, reg=tmc2209.IFCNT))["value"]
            regs = tmc2209.register_values(ax)
            for reg, value in regs:
                await self.link.request("TMC_WRITE", addr=addr, reg=reg, value=value)
            after = (await self.link.request("TMC_READ", addr=addr, reg=tmc2209.IFCNT))["value"]
            if (after - before) & 0xFF != len(regs):
                log.warning("TMC2209 %s (addr %d): IFCNT %d -> %d, some writes were lost",
                            ax.name, addr, before, after)
        except McuError as e:
            log.warning("TMC2209 %s (addr %d) not responding: %s", ax.name, addr, e)

    # ================================================================ MCU callbacks
    def _on_status(self, msg: P.Message) -> None:
        self._last_status = asyncio.get_running_loop().time()
        self.recorder.on_status()
        self.sys_flags = msg["sys_flags"]
        for ax, (pos, vel, flags) in zip(self.axes, P.status_axes(msg)):
            st = self.state[ax.name]
            if (st.flags ^ flags) & P.ST_ENDSTOP and flags & P.ST_CONFIGURED:
                log.info("endstop %s %s at %.2f", ax.name,
                         "TRIGGERED" if flags & P.ST_ENDSTOP else "released", ax.to_units(pos))
            st.pos, st.vel, st.flags = pos, vel, flags

    def _on_event(self, msg: P.Message) -> None:
        etype, axis = msg["type"], msg["axis"]
        if etype == P.EV_BOOT:
            if self.ready:
                log.warning("MCU rebooted, reconfiguring (all axes unhomed)")
            self.ready = False
            for st in self.state.values():
                st.homed = False
            self._cancel_motion()
            self._cancel_jog(self.state.keys())
            self._fail_waiters(MotionError("MCU rebooted"))
            return
        if etype == P.EV_ENDSTOP_HIT:
            ax = self.axes[axis] if axis < len(self.axes) else None
            if ax is not None and self._near_endstop_limit(ax, msg["value"]):
                log.info("%s stopped on its end-of-travel switch", ax.name)
            else:
                log.warning("%s hit its endstop outside homing (at %s)",
                            ax.name if ax else axis,
                            f"{ax.to_units(msg['value']):.2f}" if ax else msg["value"])
        elif etype == P.EV_WATCHDOG:
            log.info("MCU watchdog: motion stopped (no command received in time)")
        else:
            log.debug("MCU event %s axis=%d value=%d",
                      P.EVENT_NAMES.get(etype, etype), axis, msg["value"])
        for w in list(self._waiters):
            w_axis, types, fut = w
            if fut.done():
                self._waiters.remove(w)
            elif w_axis == axis and etype in types:
                fut.set_result(msg)
                self._waiters.remove(w)

    def _expect(self, axis_idx: int, *types: int) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((axis_idx, set(types), fut))
        return fut

    def _fail_waiters(self, exc: Exception) -> None:
        for _, _, fut in self._waiters:
            if not fut.done():
                fut.set_exception(exc)
        self._waiters.clear()

    # ================================================================ helpers
    def axis(self, name: str) -> AxisConfig:
        try:
            return self.cfg.axes[name]
        except KeyError:
            raise MotionError(f"unknown axis '{name}'") from None

    @property
    def estopped(self) -> bool:
        return bool(self.sys_flags & P.SYS_ESTOP)

    def _check_ready(self) -> None:
        if not self.ready:
            raise MotionError("MCU not ready")
        if self.estopped:
            raise MotionError("emergency stop active")

    def position(self, name: str) -> float:
        return self.axis(name).to_units(self.state[name].pos)

    def positions(self) -> Dict[str, float]:
        return {a.name: self.position(a.name) for a in self.axes}

    @staticmethod
    def _near_endstop_limit(ax: AxisConfig, steps: int) -> bool:
        """True if `steps` is at the soft limit on the switch side (+-1 unit)."""
        if not ax.endstop_guard:
            return False
        lim = ax.position_max if ax.homing_positive_dir else ax.position_min
        return abs(ax.to_units(steps) - lim) <= 1.0

    def _homed_positions(self) -> Dict[str, float]:
        return {a.name: self.position(a.name) for a in self.axes if self.state[a.name].homed}

    # ================================================================ settings
    def _load_settings(self) -> None:
        try:
            with open(self._settings_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        num = (int, float)
        # Values in axis units are dropped for an axis whose units changed
        # (zoom stepper in mm -> servo in %): speeds, limits, lenses.
        units = data.get("axis_units") or {}
        changed = {a.name for a in self.axes
                   if units.get(a.name, None if a.servo else a.units) != a.units}
        if changed:
            log.warning("units changed for %s: its saved speed, limits%s are reset",
                        ", ".join(sorted(changed)), " and lenses" if "zoom" in changed else "")
            data = dict(data)
            for key in ("speed_axis", "accel_axis"):
                data[key] = {n: v for n, v in (data.get(key) or {}).items() if n not in changed}
            lim = dict(data.get("user_limits") or {})
            lim["axes"] = {n: v for n, v in (lim.get("axes") or {}).items() if n not in changed}
            data["user_limits"] = lim
            if "zoom" in changed:
                data["lenses"], data["lens"] = {}, None
        data = self._convert_servo_ranges(data, changed)
        for n, v in (data.get("servo_pos") or {}).items():
            if n in self.cfg.axes and self.cfg.axes[n].servo and n not in changed \
                    and isinstance(v, num):
                self._servo_pos[n] = float(v)
        self.advanced = bool(data.get("advanced", False))
        # settings.json of v0.2 stored 0..1 factors: ignore those
        if isinstance(data.get("speed_all"), num):
            self.speed_all = float(data["speed_all"])
        if isinstance(data.get("accel_all"), num):
            self.accel_all = float(data["accel_all"])
        for key, dst in (("speed_axis", self.speed_axis), ("accel_axis", self.accel_axis)):
            for n, v in (data.get(key) or {}).items():
                if n in dst and isinstance(v, num):
                    dst[n] = float(v)
        for n, v in (data.get("axis_active") or {}).items():
            if n in self.axis_active and isinstance(v, bool):
                self.axis_active[n] = v
        lim = data.get("user_limits") or {}
        self.user_limits_on = bool(lim.get("enabled", False))
        for n, d in (lim.get("axes") or {}).items():
            if n in self.user_limits and isinstance(d, dict):
                for side in ("min", "max"):
                    if isinstance(d.get(side), (int, float)):
                        self.user_limits[n][side] = float(d[side])
        for k in ("smoothing", "play_speed"):
            if isinstance(data.get(k), num):
                setattr(self, k, float(data[k]))
        for n, name in (data.get("car_names") or {}).items():     # before v0.10
            if str(n).isdigit() and isinstance(name, str) and len(self.cars) < CARS:
                self.cars[int(n)] = self._new_car(name[:24])
        for c in data.get("cars") or []:
            if (isinstance(c, dict) and isinstance(c.get("car"), int)
                    and 1 <= c["car"] <= CAR_NUMBER_MAX and len(self.cars) < CARS):
                self.cars[c["car"]] = self._new_car(str(c.get("name") or "")[:24])
        for name, d in (data.get("lenses") or {}).items():
            if isinstance(d, dict) and all(isinstance(d.get(k), num) for k in
                                           ("focal_min", "focal_max", "wide", "tele")):
                lens = {k: float(d[k]) for k in ("focal_min", "focal_max", "wide", "tele")}
                lens["points"] = [[float(p[0]), float(p[1])] for p in d.get("points") or []
                                  if isinstance(p, list) and len(p) == 2
                                  and all(isinstance(x, num) for x in p)]
                lens["tilt_max"] = float(d["tilt_max"]) if isinstance(d.get("tilt_max"), num) else None
                self.lenses[str(name)] = lens
        if data.get("lens") in self.lenses:
            self.lens = data["lens"]
        lev = data.get("level") or {}
        self.level_on = bool(lev.get("enabled", False))
        for k in ("pitch", "roll"):
            if isinstance(lev.get(k), num) and abs(lev[k]) <= 45:
                setattr(self, "level_" + k, float(lev[k]))
        self.level_marks = [(float(m[0]), float(m[1])) for m in (lev.get("marks") or [])
                            if isinstance(m, list) and len(m) == 2
                            and all(isinstance(x, num) for x in m)]

    def _convert_servo_ranges(self, data: dict, changed: Set[str]) -> dict:
        """A servo's position is a % of its pulse range: when the range
        changed in the config, the saved positions (last position, lens ends
        and marks, soft limits, presets) and speeds are converted so they
        stay at the same pulse widths (same physical place)."""
        ranges = data.get("servo_range") or {}
        units = data.get("axis_units") or {}
        out = None
        for ax in self.axes:
            if not ax.servo or ax.name in changed or units.get(ax.name) is None:
                continue
            old = ranges.get(ax.name, _V0140_SERVO_RANGE)
            new = [ax.position_min, ax.position_max, ax.pulse_min, ax.pulse_max]
            try:
                p0, p1, w0, w1 = (float(x) for x in old)
            except (TypeError, ValueError):
                continue
            if [p0, p1, w0, w1] == new or p1 == p0 or w1 == w0:
                continue
            us_old = (w1 - w0) / (p1 - p0)             # us per unit
            us_new = (ax.pulse_max - ax.pulse_min) / (ax.position_max - ax.position_min)

            def conv(p, p0=p0, w0=w0, us_old=us_old, us_new=us_new, ax=ax):
                return round(ax.position_min + (w0 + (p - p0) * us_old - ax.pulse_min)
                             / us_new, 3)
            k = us_old / us_new
            n = ax.name
            log.warning("%s pulse range changed (%g..%g -> %g..%g us): saved %s positions "
                        "converted to the same pulse widths", n, w0, w1,
                        ax.pulse_min, ax.pulse_max, n)
            out = out or json.loads(json.dumps(data))
            num = (int, float)
            sp = out.get("servo_pos") or {}
            if isinstance(sp.get(n), num):
                sp[n] = conv(sp[n])
            for key in ("speed_axis", "accel_axis"):
                d = out.get(key) or {}
                if isinstance(d.get(n), num):
                    d[n] = round(d[n] * k, 3)
            u = ((out.get("user_limits") or {}).get("axes") or {}).get(n) or {}
            for side in ("min", "max"):
                if isinstance(u.get(side), num):
                    u[side] = conv(u[side])
            if n == "zoom":
                for lens in (out.get("lenses") or {}).values():
                    if not isinstance(lens, dict):
                        continue
                    for key in ("wide", "tele"):
                        if isinstance(lens.get(key), num):
                            lens[key] = conv(lens[key])
                    for p in lens.get("points") or []:
                        if isinstance(p, list) and p and isinstance(p[0], num):
                            p[0] = conv(p[0])
            self.presets.map_axis(n, conv)
        if out is not None:
            self._range_converted = True             # saved again after loading
        return data if out is None else out

    def _save_settings(self) -> None:
        data = {"advanced": self.advanced, "speed_all": self.speed_all,
                "accel_all": self.accel_all, "speed_axis": self.speed_axis,
                "accel_axis": self.accel_axis, "smoothing": self.smoothing,
                "play_speed": self.play_speed, "axis_active": self.axis_active,
                "user_limits": {"enabled": self.user_limits_on, "axes": self.user_limits},
                "cars": [{"car": n, "name": c["name"]} for n, c in self.cars.items()],
                "lenses": self.lenses, "lens": self.lens,
                "level": {"enabled": self.level_on, "pitch": self.level_pitch,
                          "roll": self.level_roll, "marks": self.level_marks},
                "axis_units": {a.name: a.units for a in self.axes},
                "servo_pos": self._servo_pos,
                "servo_range": {a.name: [a.position_min, a.position_max,
                                         a.pulse_min, a.pulse_max]
                                for a in self.axes if a.servo}}
        try:
            atomic_write_json(self._settings_path, data)
        except OSError as e:
            log.warning("cannot save settings: %s", e)

    def speed_of(self, name: str) -> float:
        """Speed used for jog (full deflection) and moves, units/s."""
        ax = self.cfg.axes[name]
        v = self.speed_axis[name] if self.advanced else self.speed_all
        return clamp(v, 1e-3, ax.max_velocity)

    def accel_of(self, name: str) -> float:
        """Acceleration used for jog and moves, units/s^2."""
        ax = self.cfg.axes[name]
        a = self.accel_axis[name] if self.advanced else self.accel_all
        return clamp(a, 1e-3, ax.max_accel)

    def set_motion(self, speed: Optional[float] = None, accel: Optional[float] = None,
                   smoothing: Optional[float] = None, advanced: Optional[bool] = None,
                   axis_speed: Optional[Dict[str, float]] = None,
                   axis_accel: Optional[Dict[str, float]] = None) -> None:
        """speed / accel: common value (units/s, units/s^2) of simple mode.
        axis_speed / axis_accel: per-axis values of advanced mode.
        smoothing: ease in/out amount (0 = sharp, 1 = softest)."""
        if advanced is not None and bool(advanced) != self.advanced:
            if advanced:     # start advanced mode from what was in effect
                for n in self.speed_axis:
                    self.speed_axis[n] = self.speed_of(n)
                    self.accel_axis[n] = self.accel_of(n)
            self.advanced = bool(advanced)
        vmax = max((a.max_velocity for a in self.axes), default=1.0)
        amax = max((a.max_accel for a in self.axes), default=1.0)
        if speed is not None:
            self.speed_all = clamp(float(speed), 1e-3, vmax)
        if accel is not None:
            self.accel_all = clamp(float(accel), 1e-3, amax)
        for n, v in (axis_speed or {}).items():
            ax = self.axis(n)
            self.speed_axis[n] = clamp(float(v), 1e-3, ax.max_velocity)
        for n, v in (axis_accel or {}).items():
            ax = self.axis(n)
            self.accel_axis[n] = clamp(float(v), 1e-3, ax.max_accel)
        if smoothing is not None:
            self.smoothing = clamp(float(smoothing), 0.0, 1.0)
        self._save_settings()

    def settings(self) -> dict:
        return {
            "advanced": self.advanced,
            "speed_all": round(self.speed_all, 3),
            "accel_all": round(self.accel_all, 3),
            "speed": {n: round(self.speed_axis[n], 3) for n in self.speed_axis},
            "accel": {n: round(self.accel_axis[n], 3) for n in self.accel_axis},
            "effective_speed": {a.name: round(self.speed_of(a.name), 3) for a in self.axes},
            "effective_accel": {a.name: round(self.accel_of(a.name), 3) for a in self.axes},
            "smoothing": self.smoothing,
            "ease_s": round(self._smooth_time(), 3),
            "play_speed": self.play_speed,
            "play_loop": self.play_loop,
        }

    # ================================================================ soft limits
    def limits_of(self, name: str) -> Tuple[float, float]:
        """Travel allowed now: configured limits, narrowed by the learned
        soft limits when they are enabled."""
        ax = self.cfg.axes[name]
        lo, hi = ax.position_min, ax.position_max
        if name == "tilt" and self.tilt_adjust:
            return lo, hi                            # finding the lens's max tilt
        lens = self._lens()
        lens_tilt = lens.get("tilt_max") if lens is not None and name == "tilt" else None
        if self.user_limits_on:
            u = self.user_limits[name]
            if u["min"] is not None:
                lo = max(lo, u["min"])
            if u["max"] is not None and lens_tilt is None:   # the lens's max replaces it
                hi = min(hi, u["max"])
        if lens_tilt is not None:
            hi = min(hi, lens_tilt)
            lo = min(lo, hi)
        if name == "zoom" and self.zoom_locked():
            p = clamp(self.position(name), ax.position_min, ax.position_max)
            return p, p                              # no lens: pinned where it is
        if name == "zoom" and self._lens() is not None:
            w, t = self.zoom_ends()                  # the ring's end stops
            lo, hi = max(lo, min(w, t)), min(hi, max(w, t))
            hi = max(lo, hi)
        return lo, hi

    async def _apply_limits(self, names: Optional[Iterable[str]] = None) -> None:
        """Send the limits in force to the MCU (it enforces them for every
        kind of motion, as the last line of defence)."""
        if not self.ready:
            return
        for name in (names or [a.name for a in self.axes]):
            ax = self.cfg.axes[name]
            if not self.state[name].homed or name in self._homing:
                continue
            lo, hi = self.limits_of(name)
            await self._send_limits(ax, lo, hi)

    async def _send_limits(self, ax: AxisConfig, lo: float, hi: float) -> None:
        """SET_LIMITS in steps. The MCU needs max > min: a pinned axis
        (servo zoom with no lens: lo == hi) gets a one-step window."""
        a = ax.to_steps(lo)
        await self.link.request("SET_LIMITS", axis=ax.index, min=a,
                                max=max(ax.to_steps(hi), a + 1), enabled=1)

    async def set_user_limit(self, name: str, side: str, value: Optional[float] = None) -> float:
        """Learn one soft limit; value defaults to the current position."""
        ax = self.axis(name)
        if side not in ("min", "max"):
            raise MotionError("side must be 'min' or 'max'")
        if value is None:
            if not self.state[name].homed:
                raise MotionError(f"axis '{name}' is not homed")
            value = self.position(name)
        value = clamp(float(value), ax.position_min, ax.position_max)
        other = self.user_limits[name]["max" if side == "min" else "min"]
        if other is not None and ((side == "min" and value >= other)
                                  or (side == "max" and value <= other)):
            raise MotionError(f"{name} {side} limit {value:.2f} must be "
                              f"{'below' if side == 'min' else 'above'} the {('max' if side == 'min' else 'min')} limit {other:.2f}")
        self.user_limits[name][side] = round(value, 3)
        self._save_settings()
        await self._apply_limits([name])
        log.info("soft limit %s %s = %.2f", name, side, value)
        return value

    async def clear_user_limit(self, name: Optional[str] = None, side: Optional[str] = None) -> None:
        names = [name] if name else list(self.user_limits)
        for n in names:
            self.axis(n)
            for sd in ([side] if side else ["min", "max"]):
                self.user_limits[n][sd] = None
        self._save_settings()
        await self._apply_limits(names)
        log.info("soft limits cleared: %s", ", ".join(names))

    async def enable_user_limits(self, on: bool) -> None:
        self.user_limits_on = bool(on)
        self._save_settings()
        await self._apply_limits()
        log.info("soft limits %s", "ENABLED" if on else "disabled")

    def limits_status(self) -> dict:
        return {"enabled": self.user_limits_on,
                "axes": {n: dict(v) for n, v in self.user_limits.items()},
                "effective": {n: list(self.limits_of(n)) for n in self.user_limits}}

    # ================================================================ base levelling
    def set_level(self, pitch: Optional[float] = None, roll: Optional[float] = None) -> None:
        """Base attitude, in tilt units: tilt that looks level at pan 0
        (pitch) and at pan +90 (roll). See level.py."""
        for k, v in (("pitch", pitch), ("roll", roll)):
            if v is not None:
                v = float(v)
                if not -45.0 <= v <= 45.0:
                    raise MotionError(f"level {k} must be within -45..45")
                setattr(self, "level_" + k, round(v, 3))
        self._level = Level(self.level_pitch, self.level_roll)
        self._level_hold = None
        self._save_settings()
        log.info("base level: pitch %.2f, roll %.2f", self.level_pitch, self.level_roll)

    def enable_level(self, on: bool) -> None:
        self.level_on = bool(on)
        self._level_hold = None
        self._save_settings()
        log.info("base levelling %s", "ON" if on else "off")

    def level_mark(self) -> dict:
        """The camera looks level right now (camera's electronic level, or
        aimed at the horizon): add a mark and recompute pitch / roll."""
        for n in ("pan", "tilt"):
            if n not in self.state:
                raise MotionError("levelling needs a pan and a tilt axis")
            if not self.state[n].homed:
                raise MotionError(f"axis '{n}' is not homed")
        p, t = round(self.position("pan"), 3), round(self.position("tilt"), 3)
        # a new mark replaces any older one taken at nearly the same pan
        self.level_marks = [m for m in self.level_marks if abs(m[0] - p) > 5.0][-7:] + [(p, t)]
        pitch, roll = level_solve(self.level_marks)
        self.set_level(pitch, roll)
        return self.level_status()

    # ================================================================ zoom lenses
    def _lens(self) -> Optional[dict]:
        return self.lenses.get(self.lens) if self.lens else None

    @property
    def lens_learning(self) -> bool:
        return time.monotonic() < self._learn_until

    def zoom_locked(self) -> bool:
        """Servo zoom with no lens selected (and no lens being learned):
        the zoom must not move (the servo would force a ring past its end)."""
        ax = self.cfg.axes.get("zoom")
        return bool(ax is not None and ax.servo and self._lens() is None
                    and not self.lens_learning)

    def _zoom_lock_note(self) -> None:
        now = time.monotonic()
        if now - self._zoom_lock_log > 5.0:
            self._zoom_lock_log = now
            log.warning(ZOOM_LOCKED)

    async def lens_learn(self, on: bool) -> None:
        """Learning a lens (servo zoom): the zoom may move over its whole
        travel with no lens selected. Ends after LENS_LEARN_TTL s unless
        renewed (the wizard renews it while open), on lens_save / select, or
        with on = False."""
        ax = self.axis("zoom")
        if on:
            first = not self.lens_learning
            self._learn_until = time.monotonic() + LENS_LEARN_TTL
            self._learning_was = True
            if first:
                log.info("lens learning: zoom free over its travel%s",
                         f" ({ax.pulse_min:g}..{ax.pulse_max:g} us)" if ax.servo else "")
                await self._apply_limits(["zoom"])
        elif self._learning_was or self.lens_learning:
            self._learn_until = 0.0
            log.info("lens learning ended")
            await self._zoom_relock()

    async def _zoom_relock(self) -> None:
        """Learning over: stop the zoom, then pin its limits where it rests
        (no lens) or set the lens's ends."""
        self._learning_was = False
        if "zoom" not in self.state or not self.ready:
            return
        if self.zoom_locked():
            self._cancel_raw(["zoom"])
            await self.stop(["zoom"])
            for _ in range(40):                      # until it rests (servo: quick)
                if not self.state["zoom"].flags & P.ST_MOVING:
                    break
                await asyncio.sleep(0.025)
        await self._apply_limits(["zoom"])

    def zoom_ends(self) -> Tuple[float, float]:
        """Zoom axis positions of the wide and tele ends (no lens: the
        configured travel, tele at the max end)."""
        lens = self._lens()
        if lens is not None:
            return lens["wide"], lens["tele"]
        ax = self.cfg.axes["zoom"]
        return ax.position_min, ax.position_max

    def zoom_sign(self) -> int:
        """+1 when the motor's positive direction zooms in (towards tele)."""
        if "zoom" not in self.cfg.axes:
            return 1
        w, t = self.zoom_ends()
        return -1 if t < w else 1

    def zoom_fraction(self) -> float:
        """0 = wide end, 1 = tele end."""
        w, t = self.zoom_ends()
        return clamp((self.position("zoom") - w) / (t - w), 0.0, 1.0) if t != w else 0.0

    async def zoom_to_fraction(self, frac: float, speed: float = 1.0, wait: bool = False) -> None:
        w, t = self.zoom_ends()
        await self.goto({"zoom": w + clamp(float(frac), 0.0, 1.0) * (t - w)},
                        speed=speed, wait=wait)

    @staticmethod
    def _focal_curve(lens: dict) -> List[Tuple[float, float]]:
        """(fraction wide -> tele, focal) through the ends and the marks."""
        w, t = lens["wide"], lens["tele"]
        mid = sorted(((p - w) / (t - w), f) for p, f in lens.get("points") or [])
        return [(0.0, lens["focal_min"])] + mid + [(1.0, lens["focal_max"])]

    @staticmethod
    def _interp(curve: List[Tuple[float, float]], x: float, i: int, j: int) -> float:
        """Piecewise linear: value j at value i = x along the curve."""
        x = clamp(x, curve[0][i], curve[-1][i])
        for a, b in zip(curve, curve[1:]):
            if x <= b[i]:
                k = (x - a[i]) / (b[i] - a[i]) if b[i] != a[i] else 0.0
                return a[j] + k * (b[j] - a[j])
        return curve[-1][j]

    def focal_now(self) -> Optional[float]:
        """Focal length (mm) of the lens fitted: interpolated between the
        marks learned on the ring (linear between the ends without marks)."""
        lens = self._lens()
        if lens is None or "zoom" not in self.state or not self.state["zoom"].homed:
            return None
        return self._interp(self._focal_curve(lens), self.zoom_fraction(), 0, 1)

    def focal_to_position(self, focal: float) -> float:
        lens = self._lens()
        if lens is None:
            raise MotionError("no lens selected: choose the lens fitted first")
        try:
            focal = float(focal)
        except (TypeError, ValueError):
            raise MotionError("focal must be a number (mm)") from None
        frac = self._interp(self._focal_curve(lens), focal, 1, 0)
        return lens["wide"] + frac * (lens["tele"] - lens["wide"])

    async def zoom_to_focal(self, focal: float, speed: float = 1.0, wait: bool = False) -> None:
        await self.goto({"zoom": self.focal_to_position(focal)}, speed=speed, wait=wait)

    async def lens_save(self, name: str, focal_min: float, focal_max: float,
                        at: Optional[str] = None, wide: Optional[float] = None,
                        tele: Optional[float] = None, points=None,
                        rename_from: Optional[str] = None) -> dict:
        """Store a lens and select it. Learning: the zoom was homed (the ring
        at the endstop end) and moved to the other end of the ring; `at`
        says which end it is now at ("tele" or "wide"). Or give the wide /
        tele positions directly. points: [[zoom position, focal], ...]
        marks in between (focal lengths printed on the barrel). The max
        tilt of a lens learned again is kept."""
        name = str(name or "").strip()[:32]
        if not name:
            raise MotionError("the lens needs a name")
        try:
            fmin, fmax = float(focal_min), float(focal_max)
        except (TypeError, ValueError):
            raise MotionError("focal lengths must be numbers (mm)") from None
        if not 0 < fmin < fmax:
            raise MotionError("min focal must be above 0 and below max focal")
        ax = self.axis("zoom")
        if wide is None and tele is None and at is None:
            # ends given as marks at the min and max focal (servo zoom)
            rest = []
            for p in points or []:
                try:
                    pos, f = float(p[0]), float(p[1])
                except (TypeError, ValueError, IndexError):
                    raise MotionError("points must be [zoom position, focal] pairs") from None
                if abs(f - fmin) < 1e-6:
                    wide = pos
                elif abs(f - fmax) < 1e-6:
                    tele = pos
                else:
                    rest.append([pos, f])
            missing = [f"{f:g} mm" for f, v in ((fmin, wide), (fmax, tele)) if v is None]
            if missing:
                raise MotionError(f"mark the zoom position of {' and '.join(missing)} "
                                  "(the ends of the zoom)")
            points = rest
        if wide is None or tele is None:
            if ax.servo:
                raise MotionError("servo zoom: mark the min and max focal positions "
                                  "(or give wide and tele)")
            if at not in ("tele", "wide"):
                raise MotionError("at must be 'tele' or 'wide': the end the lens is at now")
            if not self.state["zoom"].homed:
                raise MotionError("home the zoom first")
            here = self.position("zoom")
            home = clamp(ax.position_endstop, ax.position_min, ax.position_max)
            wide, tele = (home, here) if at == "tele" else (here, home)
        wide, tele = float(wide), float(tele)
        if abs(tele - wide) < 0.01 * (ax.position_max - ax.position_min):
            raise MotionError("the ring hardly moved: move the zoom to the other end of "
                              "the ring before saving")
        marks: Dict[float, float] = {}
        for p in points or []:
            try:
                pos, f = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError):
                raise MotionError("points must be [zoom position, focal] pairs") from None
            if not fmin < f < fmax:
                raise MotionError(f"mark {f:g} mm: must be between {fmin:g} and {fmax:g} mm")
            if not 0.0 < (pos - wide) / (tele - wide) < 1.0:
                raise MotionError(f"mark {f:g} mm: not between the two ends of the ring")
            marks[f] = round(pos, 3)                 # same focal marked again: the last
        pts = sorted(([p, f] for f, p in marks.items()),
                     key=lambda m: (m[0] - wide) / (tele - wide))
        for a, b in zip(pts, pts[1:]):
            if b[1] <= a[1]:
                raise MotionError(f"marks {a[1]:g} and {b[1]:g} mm are in the wrong order "
                                  "along the ring: mark them again")
        old = self.lenses.get(name) or (self.lenses.get(rename_from) if rename_from else None)
        self.lenses[name] = {"focal_min": fmin, "focal_max": fmax,
                             "wide": round(wide, 3), "tele": round(tele, 3), "points": pts,
                             "tilt_max": old.get("tilt_max") if old else None}
        if rename_from and rename_from != name and rename_from in self.lenses:
            del self.lenses[rename_from]
        log.info("lens '%s' %g-%g mm: wide at %.2f, tele at %.2f", name, fmin, fmax, wide, tele)
        await self.lens_select(name)
        return dict(self.lenses[name], name=name)

    async def lens_select(self, name: Optional[str]) -> None:
        """The lens fitted (None / "": none, raw motor direction and travel)."""
        if name and name not in self.lenses:
            raise MotionError(f"unknown lens '{name}'")
        self.lens = name or None
        self.tilt_adjust = False
        self._learn_until = 0.0
        self._save_settings()
        if self._learning_was or self.zoom_locked():
            await self._zoom_relock()
        await self._apply_limits([n for n in ("zoom", "tilt") if n in self.state])
        log.info("lens: %s", self.lens or "none")

    def _lens_tilt_check(self) -> dict:
        lens = self._lens()
        if lens is None:
            raise MotionError("select the lens fitted first")
        if "tilt" not in self.state:
            raise MotionError("no tilt axis")
        return lens

    async def lens_tilt_adjust(self, on: bool) -> None:
        """On: tilt free over its whole configured travel (soft limits and
        the lens's max tilt off) to find how low the camera may tilt with
        this lens. Off (cancel): limits back, the tilt returns inside them."""
        self._lens_tilt_check()
        self.tilt_adjust = bool(on)
        await self._apply_limits(["tilt"])
        log.info("lens max tilt: %s", "adjusting (tilt soft limits off)" if on else "adjust cancelled")
        if not on and self.state["tilt"].homed:
            lo, hi = self.limits_of("tilt")
            pos = self.position("tilt")
            if not lo <= pos <= hi:
                await self.goto({"tilt": clamp(pos, lo, hi)})

    async def lens_tilt_max(self, value: Optional[float] = None, clear: bool = False) -> Optional[float]:
        """Max tilt with the lens fitted: the current tilt (or `value`),
        never past the configured travel. clear: back to the soft limits."""
        lens = self._lens_tilt_check()
        ax = self.cfg.axes["tilt"]
        if clear:
            lens["tilt_max"] = None
        else:
            if value is None:
                if not self.state["tilt"].homed:
                    raise MotionError("tilt is not homed")
                value = self.position("tilt")
            lens["tilt_max"] = round(clamp(float(value), ax.position_min, ax.position_max), 3)
        self.tilt_adjust = False
        self._save_settings()
        await self._apply_limits(["tilt"])
        log.info("lens '%s' max tilt: %s", self.lens,
                 "soft limits" if lens["tilt_max"] is None else f"{lens['tilt_max']:.2f}")
        return lens["tilt_max"]

    async def lens_delete(self, name: str) -> None:
        if name not in self.lenses:
            raise MotionError(f"unknown lens '{name}'")
        del self.lenses[name]
        if self.lens == name:
            await self.lens_select(None)
        else:
            self._save_settings()

    def lens_status(self) -> dict:
        f = self.focal_now() if "zoom" in self.cfg.axes else None
        tilt = self.cfg.axes.get("tilt")
        return {"current": self.lens, "sign": self.zoom_sign(),
                "focal": round(f, 1) if f is not None else None,
                "tilt_adjust": self.tilt_adjust,
                "locked": self.zoom_locked(), "learning": self.lens_learning,
                "tilt_range": [tilt.position_min, tilt.position_max] if tilt else None,
                "list": [dict(v, name=k) for k, v in sorted(self.lenses.items())]}

    def level_clear(self) -> None:
        """Forget the marks and set the base back to level (0, 0)."""
        self.level_marks = []
        self.set_level(0.0, 0.0)

    def level_status(self) -> dict:
        out = {"enabled": self.level_on, "pitch": self.level_pitch, "roll": self.level_roll,
               "marks": [list(m) for m in self.level_marks],
               "spread": round(level_spread(self.level_marks), 1),
               "available": "pan" in self.state and "tilt" in self.state, "world": None}
        if out["available"] and self.state["pan"].homed and self.state["tilt"].homed:
            P_, T_ = self._level.to_world(self.position("pan"), self.position("tilt"))
            out["world"] = {"pan": round(P_, 3), "tilt": round(T_, 3)}
        return out

    def _level_axes(self, traj) -> bool:
        """Pan and tilt jog in the level frame this tick?"""
        if not self.level_on or self._level.identity:
            return False
        for n in ("pan", "tilt"):
            if (n not in self.state or not self.state[n].homed or not self.axis_active[n]
                    or n in self._homing or n in self._raw
                    or (traj is not None and n in traj.axes)):
                return False
        return True

    # ================================================================ axis on / off
    async def set_axis_active(self, name: str, on: bool) -> None:
        """Switch an axis on or off live (e.g. zoom drive not fitted).

        Off: it stops, its driver is unpowered, it loses its homing and is left
        out of jogs, Home all, presets and replays. On: the driver is powered
        and the axis must be homed before it moves."""
        ax = self.axis(name)
        on = bool(on)
        if on and name in self.servo_unsupported:
            raise MotionError(self.mcu_warning)
        if self.axis_active[name] == on:
            return
        if not on:
            if self._traj is not None and name in self._traj.axes:
                self._cancel_motion()
            self._cancel_jog([name])
            self._cancel_raw([name])
            self.axis_active[name] = False
            if self.ready:
                await self.link.request("STOP", axis_mask=1 << ax.index)
                await self.link.request("ENABLE", axis_mask=1 << ax.index, enable_mask=0)
            if not ax.servo:
                self.state[name].homed = False
        else:
            self.axis_active[name] = True
            if not ax.servo:
                self.state[name].homed = False
            if self.ready and self.cfg.motion.enable_on_start:
                await self.enable(True, [name])
        self._save_settings()
        log.info("axis %s %s", name, ("enabled" if ax.servo else "enabled: home it before use")
                 if on else "disabled")

    # ================================================================ blocking mode
    def lock(self, client: str, label: str) -> None:
        if self.lock_owner not in (None, client):
            raise MotionError(f"control already locked by {self.lock_label}")
        self.lock_owner, self.lock_label = client, label
        log.info("control locked by %s", label)

    def unlock(self, client: Optional[str], force: bool = False) -> None:
        if self.lock_owner is None:
            return
        if not force and client != self.lock_owner:
            raise MotionError(f"only {self.lock_label} can release the lock")
        log.info("control lock released%s", " (forced)" if force else "")
        self.lock_owner, self.lock_label = None, ""

    def may_control(self, client: Optional[str]) -> bool:
        return self.lock_owner is None or client == self.lock_owner

    def note_source(self, kind: str, ip: str) -> None:
        self.sources[(kind, ip)] = time.monotonic()

    def active_sources(self) -> List[dict]:
        now = time.monotonic()
        for k in [k for k, t in self.sources.items() if now - t > SOURCE_TTL]:
            del self.sources[k]
        return [{"kind": k, "ip": ip, "age": round(now - t, 1)}
                for (k, ip), t in sorted(self.sources.items())]

    def _smooth_time(self) -> float:
        return self.smoothing * self.cfg.motion.ease_time

    # ================================================================ jog
    def _shape(self, v: float) -> float:
        m = self.cfg.motion
        v = max(-1.0, min(1.0, float(v)))
        a = abs(v)
        if a <= m.deadband:
            return 0.0
        a = (a - m.deadband) / (1.0 - m.deadband)
        a = (1.0 - m.expo) * a + m.expo * a ** 3
        return math.copysign(a, v)

    def jog(self, values: Dict[str, float], scale=None) -> List[str]:
        """Normalized velocity command (-1..1) per axis. Must be refreshed
        by the caller at least every motion.jog_timeout seconds.

        scale: optional speed factor (0..1), a number or {axis: factor},
        applied after the deadband / expo curve (e.g. a controller's speed
        slider: a slow setting must not fall into the deadband).

        A non-zero jog on an axis that is running a move or a replay takes
        over (manual override). Zero values never interrupt a move.
        Returns the axes refused because they are not homed."""
        if not self.ready or self.estopped:
            return []
        now = asyncio.get_running_loop().time()
        if "zoom" in values and self.zoom_sign() < 0:    # +: zoom in (tele), any lens
            values = {**values, "zoom": -float(values["zoom"])}
        if "zoom" in values and self.zoom_locked():
            if values["zoom"]:
                self._zoom_lock_note()
            values = {n: v for n, v in values.items() if n != "zoom"}
        shaped = {n: self._shape(v) for n, v in values.items()
                  if n in self.state and n not in self._homing and n not in self._raw
                  and self.axis_active.get(n)}
        if scale is not None:
            for n in shaped:
                k = scale.get(n, 1.0) if isinstance(scale, dict) else scale
                try:
                    shaped[n] *= clamp(float(k), 0.0, 1.0)
                except (TypeError, ValueError):
                    pass
        refused = []
        if self.cfg.motion.require_homing:
            refused = [n for n, v in shaped.items() if v != 0.0 and not self.state[n].homed]
            shaped = {n: v for n, v in shaped.items() if self.state[n].homed}
            if refused and now - self._refused_log > 2.0:
                self._refused_log = now
                log.warning("jog refused: %s not homed (home first)", ", ".join(refused))
        if self._traj is not None and any(v != 0.0 and n in self._traj.axes
                                          for n, v in shaped.items()):
            log.info("manual jog: %s interrupted", self._traj_kind or "move")
            self._cancel_motion(handover=True)
        for name, v in shaped.items():
            if self._traj is not None and name in self._traj.axes:
                continue
            st = self.state[name]
            st.jog_value, st.jog_time, st.jog_active = v, now, True
        return refused

    def _cancel_jog(self, names: Iterable[str]) -> None:
        for n in names:
            st = self.state[n]
            st.jog_active, st.jog_value = False, 0.0
            self._shapers[n].reset(0.0)

    # ================================================================ streaming
    async def _stream_loop(self) -> None:
        loop = asyncio.get_running_loop()
        period = 1.0 / self.cfg.motion.stream_rate
        next_t = last = loop.time()
        while True:
            next_t += period
            delay = next_t - loop.time()
            if delay < -0.1:                          # fell far behind: resync
                next_t = loop.time()
            await asyncio.sleep(max(0.0, delay))
            now = loop.time()
            dt, last = min(0.05, now - last), now
            if not self.ready:
                continue
            if self.estopped:
                if self._traj is not None:
                    self._cancel_motion()
                    self._cancel_jog(self.state.keys())
                continue
            try:
                self._stream_tick(now, dt)
            except Exception:  # noqa: BLE001
                log.exception("stream tick failed")

    def _desired_at(self, t: float) -> Optional[Dict[str, float]]:
        for ts, pos in reversed(self._des_hist):
            if ts <= t:
                return pos
        return None

    def _clamp_desired(self, name: str, p: float) -> float:
        """Desired position as the MCU will allow it: inside the limits, or
        on the way back in when the head starts outside them (limits just
        narrowed, e.g. a lens's max tilt): no correction beyond the move."""
        lo, hi = self.limits_of(name)
        here = self.position(name)
        return clamp(p, min(lo, here), max(hi, here))

    def _stream_tick(self, now: float, dt: float) -> None:
        mask, vels = 0, [0.0] * P.MAX_AXES
        traj = self._traj
        if traj is not None:
            traj.advance(dt)
            samples = traj.sample()
            self._des_hist.append((now, {n: self._clamp_desired(n, p)
                                         for n, (p, _) in samples.items()}))
            ref = None
            if now - self._last_status < 0.1:
                ref = self._desired_at(self._last_status - FEEDBACK_LATENCY)
            for name, (p, v) in samples.items():
                ax = self.cfg.axes[name]
                if ref is not None and name in ref:
                    corr = KP * (ref[name] - self.position(name))
                    v += clamp(corr, -0.1 * ax.max_velocity, 0.1 * ax.max_velocity)
                v = clamp(v, -ax.max_velocity, ax.max_velocity)
                if self.state[name].homed:          # never through the limits
                    lo, hi = self.limits_of(name)
                    pos = self.position(name)
                    v = clamp(v, -braking_speed(pos - lo, ax.max_accel, 0.0),
                              braking_speed(hi - pos, ax.max_accel, 0.0))
                self._cmd_vel[name] = v
                mask |= 1 << ax.index
                vels[ax.index] = v * ax.steps_per_unit
            if traj.done:
                self._finish_traj(traj)

        timeout = self.cfg.motion.jog_timeout
        smooth = self._smooth_time()
        mixed = self._level_axes(traj)
        world: Dict[str, float] = {}
        for ax in self.axes:
            name = ax.name
            if (traj is not None and name in traj.axes) or name in self._homing or name in self._raw:
                continue
            st, sh = self.state[name], self._shapers[name]
            mix = mixed and name in ("pan", "tilt")
            if not st.jog_active and sh.v == 0.0 and not mix:
                continue
            if st.jog_active and now - st.jog_time > timeout:
                st.jog_active, st.jog_value = False, 0.0
            amax = self.accel_of(name)
            jerk = amax / smooth if smooth > 1e-3 else math.inf
            target = st.jog_value * self.speed_of(name)
            st.braking = False
            if mix:                                   # level frame rate, mixed below
                world[name] = sh.step(target, amax, jerk, dt)
                continue
            if st.homed:                              # ease into the soft limits
                pos = self.position(name)
                lo, hi = self.limits_of(name)
                up = braking_speed(hi - pos, amax, smooth)
                dn = braking_speed(pos - lo, amax, smooth)
                if target > up or target < -dn:
                    target = clamp(target, -dn, up)
                    st.braking = True
            v = sh.step(target, amax, jerk, dt)
            self._cmd_vel[name] = v
            mask |= 1 << ax.index
            vels[ax.index] = v * ax.steps_per_unit
        if mixed:
            mask |= self._level_tick(world, smooth, vels, now, dt)
        else:
            self._level_hold = None
        if mask:
            self.link.send("SET_VELOCITY", axis_mask=mask, v0=vels[0], v1=vels[1],
                           v2=vels[2], v3=vels[3])

    def _level_tick(self, world: Dict[str, float], smooth: float, vels: List[float],
                    now: float, dt: float) -> int:
        """Level-frame jog rates -> pan / tilt motor velocities. While only
        pan is jogged, the level tilt is held where it was."""
        wp, wt = world.get("pan", 0.0), world.get("tilt", 0.0)
        # where the head will be while this command runs (the measure is a
        # few ms old; at speed that would bend the level path)
        ahead = clamp(now - self._last_status, 0.0, 0.1) + FEEDBACK_LATENCY + 0.5 * dt
        p = self.position("pan") + self._cmd_vel["pan"] * ahead
        t = self.position("tilt") + self._cmd_vel["tilt"] * ahead
        if wp == 0.0 or wt != 0.0:
            self._level_hold = None
        elif self._level_hold is None:
            self._level_hold = self._level.to_world(p, t)[1]
        if wp == 0.0 and wt == 0.0:
            vm = {"pan": 0.0, "tilt": 0.0}
        else:
            vp, vt = self._level.mount_rates(p, t, wp, wt, self._level_hold)
            vm = {"pan": vp, "tilt": vt}
        # same caps as a plain jog (axis maximum, easing into the soft
        # limits), applied to both axes together: the aim keeps to the level
        # path, it stops at a limit instead of leaving it
        k = 1.0
        for name, v in vm.items():
            st = self.state[name]
            pos = self.position(name)
            lo, hi = self.limits_of(name)
            amax = self.accel_of(name)
            cap = min(self.cfg.axes[name].max_velocity,
                      braking_speed(hi - pos, amax, smooth) if v > 0
                      else braking_speed(pos - lo, amax, smooth))
            st.braking = abs(v) > cap and cap < self.cfg.axes[name].max_velocity
            if abs(v) > cap:
                k = min(k, cap / abs(v))
        mask = 0
        for name, v in vm.items():
            ax = self.cfg.axes[name]
            v *= k
            if v == 0.0 and self._cmd_vel[name] == 0.0:
                continue                      # idle: nothing to send
            self._cmd_vel[name] = v
            mask |= 1 << ax.index
            vels[ax.index] = v * ax.steps_per_unit
        return mask

    # ================================================================ trajectories
    def _start_traj(self, traj, kind: str, replace: bool = False) -> asyncio.Future:
        """replace: the previous trajectory is superseded on purpose (its
        future resolves to False instead of failing with 'move interrupted')."""
        self._cancel_traj(quiet=replace)
        self._cancel_jog(traj.axes)
        self._des_hist.clear()
        fut = asyncio.get_running_loop().create_future()
        self._traj, self._traj_kind, self._traj_future = traj, kind, fut
        log.debug("%s started: %s (%.2f s)", kind,
                  {n: round(v, 2) for n, v in traj.final.items()}, traj.duration)
        return fut

    def _finish_traj(self, traj) -> None:
        fut = self._traj_future
        self._traj, self._traj_kind, self._traj_future = None, "", None
        for n in traj.axes:
            self._cmd_vel[n] = 0.0
        asyncio.get_running_loop().create_task(self._snap(traj, fut))

    async def _snap(self, traj, fut: Optional[asyncio.Future]) -> None:
        """Land exactly on the final positions with an MCU position move."""
        try:
            waits = []
            for name, target in traj.final.items():
                ax = self.cfg.axes[name]
                spu = ax.steps_per_unit
                # landing on an end-of-travel switch halts the axis: that is "done" too
                waits.append(self._expect(ax.index, P.EV_MOVE_DONE, P.EV_ENDSTOP_HIT))
                target = clamp(target, *self.limits_of(name))
                await self.link.request("MOVE_TO", axis=ax.index, target=ax.to_steps(target),
                                        max_vel=ax.max_velocity * spu * 0.5,
                                        accel=ax.max_accel * spu)
            await asyncio.wait_for(asyncio.gather(*waits), 3.0)
            if fut is not None and not fut.done():
                fut.set_result(True)
        except Exception as e:  # noqa: BLE001
            if fut is not None and not fut.done():
                fut.set_exception(MotionError(f"move did not complete: {e}"))

    def _cancel_traj(self, handover: bool = False, quiet: bool = False) -> None:
        """Drop the running trajectory. Its axes are handed to the jog
        shapers at their streamed speed: they come to a smooth stop (or follow
        the jog, with handover) unless a new trajectory takes them over or a
        STOP / E-stop zeroes them. Never leave them at 0 silently: the board
        would keep the last streamed velocity while other axes are streamed."""
        traj, fut = self._traj, self._traj_future
        self._traj, self._traj_kind, self._traj_future = None, "", None
        if traj is not None:
            for n in traj.axes:
                self._shapers[n].reset(self._cmd_vel[n])
                if not handover and self._cmd_vel[n] != 0.0:
                    st = self.state[n]
                    st.jog_active, st.jog_value = False, 0.0
        if fut is not None and not fut.done():
            if quiet:
                fut.set_result(False)
            else:
                fut.set_exception(MotionError("move interrupted"))
                fut.exception()     # its awaiter (if any) still gets it; no log noise

    def _current_state(self, axes: Iterable[str]) -> Tuple[Dict[str, float], Dict[str, float]]:
        """Where the head is going right now: the streamed set point and
        velocity if a trajectory runs (no measurement lag), else measured."""
        pos, vel = {}, {}
        sample = self._traj.sample() if self._traj is not None else {}
        for n in axes:
            if n in sample:
                pos[n], vel[n] = sample[n][0], self._cmd_vel[n]
            else:
                pos[n] = self.position(n)
                vel[n] = self.state[n].vel / self.cfg.axes[n].steps_per_unit
        return pos, vel

    def _cancel_motion(self, handover: bool = False) -> None:
        """Stop any running move and replay (jog sources are kept)."""
        if self._play_task is not None and not self._play_task.done():
            self._play_task.cancel()
        self._play_task = None
        self.playback = None
        if self._track_task is not None and not self._track_task.done():
            self._track_task.cancel()
        self._track_task = None
        if self.tracking["phase"] != "idle":
            self.tracking["phase"] = "idle"
        self._cancel_traj(handover)

    # ================================================================ moves
    async def goto(self, targets: Dict[str, float], speed: float = 1.0,
                   wait: bool = False) -> None:
        """Absolute move in user units. All axes arrive at the same time,
        with the current acceleration and ease in/out settings."""
        traj = self._plan_move(targets, speed)
        if traj is None:
            return
        fut = self._start_traj(traj, "move")
        if wait:
            await fut
        else:
            fut.add_done_callback(lambda f: f.cancelled() or f.exception())

    def _plan_move(self, targets: Dict[str, float], speed: float = 1.0) -> Optional[MoveTrajectory]:
        self._check_ready()
        k = max(0.01, min(1.0, speed))
        start, final, vmax, amax = {}, {}, {}, {}
        skipped = [n for n in targets if n in self.state and not self.axis_active[n]]
        targets = {n: v for n, v in targets.items() if n not in skipped}
        if skipped and not targets:
            raise MotionError(f"{', '.join(skipped)} is disabled")
        if "zoom" in targets and self.zoom_locked():
            # a preset still moves pan / tilt, never the zoom
            targets = {n: v for n, v in targets.items() if n != "zoom"}
            if not targets:
                raise MotionError(ZOOM_LOCKED)
            self._zoom_lock_note()
        for name, target in targets.items():
            ax = self.axis(name)
            if not self.state[name].homed:
                raise MotionError(f"axis '{name}' is not homed")
            if name in self._homing:
                raise MotionError(f"axis '{name}' is homing")
            final[name] = clamp(float(target), *self.limits_of(name))
            start[name] = self.position(name)
            vmax[name] = self.speed_of(name) * k
            amax[name] = self.accel_of(name)
        if not final:
            return None
        return MoveTrajectory(start, final, vmax, amax, self._smooth_time())

    async def move_relative(self, deltas: Dict[str, float], speed: float = 1.0,
                            wait: bool = False) -> None:
        await self.goto({n: self.position(n) + d for n, d in deltas.items()},
                        speed=speed, wait=wait)

    # ================================================================ raw moves
    RAW_MAX_STEPS = 10_000_000

    async def raw_move(self, name: str, steps: int, speed: float,
                       accel: Optional[float] = None, wait: bool = False) -> dict:
        """Wiring check (mount dismantled): turn an axis by a number of
        microsteps at `speed` microsteps/s, without homing, through the soft
        limits and the travel. The axis loses its homing. The endstop guard
        still halts it on its switch (moving toward the homing side). A move
        sent while one runs adds to its target. Speed and acceleration are
        capped by the axis's max_velocity / max_accel (MCU caps)."""
        ax = self.axis(name)
        self._check_ready()
        if not self.axis_active[name]:
            raise MotionError(f"{name} is off: switch it on first")
        if name in self._homing:
            raise MotionError(f"axis '{name}' is homing")
        st = self.state[name]
        if not st.flags & P.ST_ENABLED:
            raise MotionError(f"{name} driver is not powered (motors off): power it first")
        steps = int(steps)
        if steps == 0 or abs(steps) > self.RAW_MAX_STEPS:
            raise MotionError(f"steps must be 1..{self.RAW_MAX_STEPS:,} either way")
        spu = ax.steps_per_unit
        vcap, acap = ax.max_velocity * spu, ax.max_accel * spu
        speed = float(speed)
        if not speed > 0:
            raise MotionError("speed must be above 0 steps/s")
        v = min(speed, vcap)
        a = acap if accel is None else min(max(float(accel), 1.0), acap)
        guard = ((1 if ax.homing_positive_dir else -1)
                 if ax.has_endstop and ax.endstop_guard else 0)
        if guard and steps * guard > 0 and st.flags & (P.ST_ENDSTOP | P.ST_HALTED):
            raise MotionError(f"{name} endstop is triggered: it only moves away from the "
                              f"switch ({'-' if guard > 0 else '+'} steps)")
        if self._traj is not None and name in self._traj.axes:
            self._cancel_motion()
        self._cancel_jog([name])
        prev = self._raw.pop(name, None)
        if prev is not None:
            prev["task"].cancel()
        start = prev["target"] if prev is not None else st.pos
        target = start + steps
        if ax.servo:
            # never outside the limits in force: the lens's ring ends and the
            # pulse range (the servo would force), no motion with no lens
            if name == "zoom" and self.zoom_locked():
                raise MotionError(ZOOM_LOCKED)
            lo, hi = sorted(ax.to_steps(x) for x in self.limits_of(name))
            target = int(clamp(target, lo, hi))
            if target == st.pos:
                lens = f", lens {self.lens}" if name == "zoom" and self.lens else ""
                raise MotionError(f"{name} is at the end of its range "
                                  f"({ax.pulse_us(ax.to_units(lo)):g}.."
                                  f"{ax.pulse_us(ax.to_units(hi)):g} us{lens})")
        else:
            if st.homed:
                log.info("%s: raw move, homing lost (home it again before use)", name)
            st.homed = False
            await self.link.request("SET_LIMITS", axis=ax.index, min=0, max=0, enabled=0)
        fut = self._expect(ax.index, P.EV_MOVE_DONE, P.EV_ENDSTOP_HIT)
        await self.link.request("MOVE_TO", axis=ax.index, target=target, max_vel=v, accel=a)
        timeout = abs(target - st.pos) / v + 2.0 * v / a + 3.0
        info = {"axis": name, "start": st.pos, "target": target, "speed": round(v, 1),
                "accel": round(a, 1), "capped": speed > vcap, "result": None}
        log.info("%s raw move %+d steps at %.0f steps/s%s (target %d)", name, steps, v,
                 f" (capped by max_velocity {vcap:.0f})" if speed > vcap else "", target)
        info["task"] = asyncio.get_running_loop().create_task(
            self._raw_wait(name, ax, fut, info, timeout))
        self._raw[name] = info
        if wait:
            await asyncio.wait([info["task"]])      # a Stop ends it: no exception
        return {k: v for k, v in info.items() if k != "task"}

    async def _raw_wait(self, name: str, ax: AxisConfig, fut: asyncio.Future,
                        info: dict, timeout: float) -> None:
        try:
            ev = await asyncio.wait_for(fut, timeout)
            if ev["type"] == P.EV_ENDSTOP_HIT:
                info["result"] = "endstop"
                log.warning("%s raw move halted by the endstop at %d steps", name, ev["value"])
            else:
                info["result"] = "done"
                log.info("%s raw move done at %d steps", name, ev["value"])
        except asyncio.TimeoutError:
            info["result"] = "timeout"
            log.warning("%s raw move did not finish in time: stopped", name)
            try:
                await self.link.request("STOP", axis_mask=1 << ax.index)
            except Exception:  # noqa: BLE001
                pass
        except MotionError as e:
            info["result"] = str(e)
        except asyncio.CancelledError:
            info["result"] = "stopped"
            raise
        finally:
            if not fut.done():
                fut.cancel()
            if self._raw.get(name) is info:
                del self._raw[name]

    def _cancel_raw(self, names: Iterable[str]) -> None:
        for n in names:
            info = self._raw.pop(n, None)
            if info is not None:
                info["task"].cancel()

    async def stop(self, names: Optional[Iterable[str]] = None) -> None:
        names = list(names) if names else [a.name for a in self.axes]
        self._cancel_raw(names)
        self._cancel_motion()
        self._cancel_jog(names)
        mask = 0
        for n in names:
            mask |= 1 << self.axis(n).index
        await self.link.request("STOP", axis_mask=mask)

    async def estop(self) -> None:
        self._cancel_raw(list(self._raw))
        self._cancel_motion()
        if self.recorder.active:
            self.recorder.cancel()
        self._cancel_jog(self.state.keys())
        await self.link.request("ESTOP", retries=3)
        self._fail_waiters(MotionError("emergency stop"))
        log.warning("EMERGENCY STOP")

    async def clear_estop(self) -> None:
        await self.link.request("CLEAR_ESTOP")
        self.sys_flags &= ~P.SYS_ESTOP       # don't wait for the next STATUS
        self.last_error = ""
        log.info("emergency stop cleared")

    # ================================================================ diagnostics
    async def diagnostics(self) -> dict:
        """Endstop states plus the TMC2209 view of its own pins (IOIN).

        IOIN.diag shows the driver's DIAG output. On the SKR Pico a DIAG jumper
        connects it to the endstop input of the same axis, which masks the switch.
        """
        out = {}
        for ax in self.axes:
            st = self.state[ax.name]
            d = {"endstop_triggered": bool(st.flags & P.ST_ENDSTOP),
                 "endstop_pin": f"gpio{ax.endstop_pin.gpio}" if ax.has_endstop else None,
                 "endstop_invert": ax.endstop_pin.invert,
                 "endstop_pullup": ax.endstop_pin.pullup,
                 "endstop_guard": ax.endstop_guard,
                 "enabled": bool(st.flags & P.ST_ENABLED)}
            if ax.tmc is not None and self.cfg.tmc_uart is not None:
                regs = {}
                for name, reg in (("GCONF", tmc2209.GCONF), ("GSTAT", tmc2209.GSTAT),
                                  ("IFCNT", tmc2209.IFCNT), ("IOIN", tmc2209.IOIN),
                                  ("DRV_STATUS", tmc2209.DRV_STATUS)):
                    r = await self.link.request("TMC_READ", addr=ax.tmc.uart_address, reg=reg)
                    regs[name] = r["value"] if r["ok"] else None
                d["tmc_registers"] = {k: (None if v is None else f"0x{v:08x}")
                                      for k, v in regs.items()}
                if regs["IOIN"] is not None:
                    d["tmc_pins"] = tmc2209.decode_ioin(regs["IOIN"])
                if regs["DRV_STATUS"] is not None:
                    d["tmc_status"] = tmc2209.decode_drv_status(regs["DRV_STATUS"])
                if regs["GSTAT"] is not None:
                    d["tmc_gstat"] = {"reset": bool(regs["GSTAT"] & 1),
                                      "drv_err": bool(regs["GSTAT"] & 2),
                                      "uv_cp": bool(regs["GSTAT"] & 4)}
            out[ax.name] = d
        return out

    async def enable(self, on: bool, names: Optional[Iterable[str]] = None) -> None:
        names = list(names) if names else [a.name for a in self.axes]
        if on:                                   # inactive axes stay unpowered
            names = [n for n in names if self.axis_active.get(n)]
            if not names:
                return
        mask = 0
        for n in names:
            mask |= 1 << self.axis(n).index
            if not on:
                if not self.cfg.axes[n].servo:  # a servo stays absolute
                    self.state[n].homed = False     # may move freely while unpowered
                self._cancel_jog([n])
                self._cancel_raw([n])
        await self.link.request("ENABLE", axis_mask=mask, enable_mask=mask if on else 0)

    # ================================================================ homing
    # ================================================================ shutdown
    # Command that powers the Pi off. Needs the sudoers rule installed by
    # deploy/setup-remote.sh. Fixed here on purpose (not in the editable
    # config: the web config page must not be able to run commands).
    POWEROFF_CMD = ("sudo", "-n", "/usr/bin/systemctl", "poweroff")

    def shutdown_targets(self) -> Dict[str, float]:
        """Resting position of every homed, active axis that has one."""
        out = {}
        for ax in self.axes:
            sd = ax.shutdown_position
            if sd is None or not self.axis_active[ax.name] or not self.state[ax.name].homed:
                continue
            lo, hi = self.limits_of(ax.name)
            out[ax.name] = lo if sd == "min" else hi if sd == "max" else clamp(float(sd), lo, hi)
        return out

    def _sd_phase(self, phase: str, message: str) -> None:
        self.shutdown_state = {"phase": phase, "message": message}
        log.warning("shutdown: %s", message)

    def claim_shutdown(self) -> None:
        """Synchronous part of a shutdown request: only one at a time."""
        if self.shutdown_state is not None and self.shutdown_state["phase"] != "failed":
            raise MotionError("shutdown already in progress")
        self._sd_phase("parking", "moving to the rest position")

    async def shutdown(self, poweroff: bool = True) -> None:
        """Park the head, turn the motors off, flush every file to the SD
        card, then power the Pi off. If the Pi cannot be powered off, the
        state ends as "safe": nothing is written any more and the mount can
        be unplugged."""
        self.claim_shutdown()
        await self.run_shutdown(poweroff)

    async def run_shutdown(self, poweroff: bool = True) -> None:
        """The shutdown sequence, after claim_shutdown()."""
        try:
            self._cancel_motion()
            if self.recorder.active:
                self.recorder.cancel()
            targets = self.shutdown_targets() if self.ready and not self.estopped else {}
            if targets:
                log.info("shutdown: resting at %s",
                         ", ".join(f"{n} {v:g}" for n, v in targets.items()))
                traj = self._plan_move(targets)
                if traj is not None:
                    try:
                        await asyncio.wait_for(self._start_traj(traj, "park", replace=True), 60)
                    except (asyncio.TimeoutError, MotionError) as e:
                        log.warning("shutdown: rest move incomplete (%s)", e)
            self._sd_phase("motors_off", "turning the motors off")
            if self.ready:
                try:
                    await self.enable(False)
                except (McuError, MotionError, asyncio.TimeoutError) as e:
                    log.warning("shutdown: cannot turn the motors off: %s", e)
            self._save_settings()
            await asyncio.get_running_loop().run_in_executor(None, sync_all)
        except Exception as e:  # noqa: BLE001
            self._sd_phase("failed", f"shutdown failed: {e}")
            raise
        if not poweroff:
            self._sd_phase("safe", "motors off, files saved: safe to unplug")
            return
        self._sd_phase("powering_off", "powering the Raspberry Pi off")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.POWEROFF_CMD, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), 15)
            if proc.returncode != 0:
                text = out.decode(errors="replace").strip().splitlines()
                raise OSError(text[0] if text else f"exit {proc.returncode}")
        except (OSError, asyncio.TimeoutError) as e:
            self._sd_phase("safe", "motors off, files saved: safe to unplug "
                                   f"(the Pi could not power itself off: {e})")

    # ================================================================ homing
    async def home(self, names: Optional[Iterable[str]] = None) -> None:
        """Home the given axes, or every axis with home_with_all = True."""
        self.last_error = ""
        if names:
            names = list(names)
            off = [n for n in names if not self.axis_active.get(n, True)]
            if off:
                raise MotionError(f"{', '.join(off)} is disabled: enable it first")
        else:
            names = [a.name for a in self.axes if a.home_with_all and self.axis_active[a.name]]
        self._check_ready()
        if not names:
            raise MotionError("no axis to home")
        parks = await asyncio.gather(*(self._home_axis(self.axis(n)) for n in names))
        # one combined move to the park positions: a move per axis would
        # interrupt each other (a single trajectory runs at a time)
        targets = {n: t for n, t in zip(names, parks) if t is not None}
        if targets:
            log.info("moving to %s after homing",
                     ", ".join(f"{n} {t:g}" for n, t in targets.items()))
            if self._traj_kind == "park":
                # another axis homed just before is still parking: one move
                # takes both there (that homing ends quietly, not interrupted)
                targets = {**self._traj.final, **targets}
            traj = self._plan_move(targets)
            if traj is not None:
                await self._start_traj(traj, "park", replace=True)

    async def _home_axis(self, ax: AxisConfig) -> Optional[float]:
        """Home one axis. Returns where it should park (or None)."""
        st = self.state[ax.name]
        if ax.servo:                     # absolute: always "homed", never moves here
            if ax.name in self.servo_unsupported:
                raise MotionError(self.mcu_warning)
            st.homed = True
            return None
        if ax.name in self._homing:
            raise MotionError(f"axis '{ax.name}' already homing")
        self._homing.add(ax.name)
        self._cancel_jog([ax.name])
        self._cancel_raw([ax.name])
        st.homed = False
        spu = ax.steps_per_unit
        try:
            await self.link.request("SET_LIMITS", axis=ax.index, min=0, max=0, enabled=0)
            if not ax.has_endstop:
                # No switch: current position is declared to be position_endstop
                await self._set_position(ax, ax.position_endstop)
            else:
                d = 1 if ax.homing_positive_dir else -1
                travel = ax.to_steps((ax.position_max - ax.position_min) * 1.5
                                     + ax.homing_retract_dist)
                log.info("homing %s", ax.name)
                trig = await self._home_approach(ax, d * ax.homing_speed * spu, travel)
                retract = max(1, ax.to_steps(ax.homing_retract_dist))
                await self._home_retract(ax, trig, d, retract, travel)
                await self._home_approach(ax, d * ax.second_homing_speed * spu, retract * 3)
                await self._set_position(ax, ax.position_endstop)
            lo, hi = self.limits_of(ax.name)
            await self._send_limits(ax, lo, hi)
            st.homed = True
            log.info("%s homed", ax.name)
        finally:
            self._homing.discard(ax.name)
        target = ax.park_position
        lo, hi = self.limits_of(ax.name)
        if target is None and not lo <= ax.position_endstop <= hi:
            target = ax.position_endstop
        return None if target is None else clamp(target, lo, hi)

    # ================================================================ servo axes
    def _servo_start(self, ax: AxisConfig) -> float:
        """Where a servo axis is commanded after (re)configuration: its last
        position (no jump), else park_position, else the middle."""
        p = self._servo_pos.get(ax.name)
        if p is None:
            p = ax.park_position
        if p is None:
            p = (ax.position_min + ax.position_max) / 2
        return clamp(p, ax.position_min, ax.position_max)

    async def _servo_init(self, ax: AxisConfig) -> None:
        """A servo is absolute (its own position loop): no homing. The MCU
        position is set before the output is enabled."""
        start = self._servo_start(ax)
        await self._set_position(ax, start)
        self.state[ax.name].homed = True
        lo, hi = ((start, start) if ax.name == "zoom" and self.zoom_locked()
                  else self.limits_of(ax.name))
        await self._send_limits(ax, lo, hi)

    def _servo_persist(self) -> None:
        """Remember where each servo rests (settings.json, at most every 2 s)."""
        now = time.monotonic()
        if now - self._servo_saved < 2.0:
            return
        changed = False
        for ax in self.axes:
            st = self.state[ax.name]
            if not ax.servo or not st.homed or st.flags & P.ST_MOVING:
                continue
            p = round(self.position(ax.name), 2)
            if abs(p - self._servo_pos.get(ax.name, math.inf)) >= 0.05:
                self._servo_pos[ax.name] = p
                changed = True
        if changed:
            self._servo_saved = now
            self._save_settings()

    async def _set_position(self, ax: AxisConfig, units: float) -> None:
        steps = ax.to_steps(units)
        await self.link.request("SET_POSITION", axis=ax.index, position=steps)
        self.state[ax.name].pos = steps      # don't wait for the next STATUS

    async def _home_retract(self, ax: AxisConfig, trig: int, d: int, retract: int,
                            travel: int) -> None:
        """Back off from the switch until it is released, then retract once more.

        Handles both a momentary switch and a cam/flag that stays pressed over
        a whole region (the axis may start inside that region).
        """
        spu = ax.steps_per_unit
        target = trig
        settle = 2.5 / self.cfg.mcu.status_rate
        while True:
            target -= d * retract
            if abs(target - trig) > travel:
                raise MotionError(f"homing {ax.name}: endstop never released")
            fut = self._expect(ax.index, P.EV_MOVE_DONE)
            await self.link.request("MOVE_TO", axis=ax.index, target=target,
                                    max_vel=ax.homing_speed * spu, accel=ax.max_accel * spu)
            await asyncio.wait_for(fut, 30)
            await asyncio.sleep(settle)                # fresh STATUS with endstop state
            if not self.state[ax.name].flags & P.ST_ENDSTOP:
                return

    async def _home_approach(self, ax: AxisConfig, velocity: float, travel: int) -> int:
        fut = self._expect(ax.index, P.EV_HOME_TRIGGERED, P.EV_HOME_FAILED)
        await self.link.request("HOME", axis=ax.index, velocity=velocity, max_travel=travel)
        timeout = travel / max(abs(velocity), 1.0) + 5.0
        ev = await asyncio.wait_for(fut, timeout)
        if ev["type"] == P.EV_HOME_FAILED:
            raise MotionError(f"homing {ax.name}: endstop not found")
        return ev["value"]

    # ================================================================ presets
    def save_preset(self, pid, name: str = "") -> None:
        """Store the current position of every homed axis."""
        pos = {n: p for n, p in self.positions().items() if self.state[n].homed}
        if not pos:
            raise MotionError("no homed axis: home before saving presets")
        self.presets.set(pid, pos, name)

    async def recall_preset(self, pid, speed: float = 1.0, wait: bool = False) -> None:
        pos = self.presets.get(pid)
        if pos is None:
            raise MotionError(f"preset {pid} not defined")
        await self.goto({k: v for k, v in pos.items() if k in self.state},
                        speed=speed, wait=wait)

    # ================================================================ recording / replay
    def record_start(self, mode: str = "continuous", name: str = "",
                     on_move: bool = False) -> None:
        self.recorder.start(mode, name, on_move)
        log.info("recording %s (%s)", "armed: starts on first movement" if on_move
                 else "started", mode)

    def save_edited_recording(self, name: str, data) -> dict:
        limits = {a.name: (a.position_min, a.position_max) for a in self.axes}
        summary = self.recorder.save_edited(name, data, limits)
        if not name.startswith("~"):
            log.info("recording '%s' saved from the editor: %d points, %.1f s",
                     summary["name"], summary["points"], summary["duration"])
        return summary

    def import_recording(self, data, name: str = "") -> dict:
        limits = {a.name: (a.position_min, a.position_max) for a in self.axes}
        summary = self.recorder.import_data(data, limits, name)
        log.info("recording '%s' uploaded: %d points, %.1f s", summary["name"],
                 summary["points"], summary["duration"])
        return summary

    def record_keypoint(self) -> int:
        n = self.recorder.keypoint()
        log.info("keypoint %d recorded", n)
        return n

    def record_stop(self) -> dict:
        summary = self.recorder.stop()
        log.info("recording '%s' saved: %d points, %.1f s", summary["name"],
                 summary["points"], summary["duration"])
        return summary

    def set_play(self, speed: Optional[float] = None, loop: Optional[bool] = None) -> None:
        if speed is not None:
            self.play_speed = clamp(float(speed), 0.1, 4.0)
            self._save_settings()
        if loop is not None:
            self.play_loop = bool(loop)
        if self.playback is not None:
            self.playback["loop"] = self.play_loop

    async def play(self, name: str, speed: Optional[float] = None,
                   loop: Optional[bool] = None) -> None:
        """Start replaying a recording (returns immediately)."""
        self._check_ready()
        rec = self.recorder.load(name)
        axes = [n for n in rec["points"][0]["pos"]
                if n in self.state and self.axis_active[n]]
        if not axes:
            raise MotionError("recording uses no configured axis")
        for n in axes:
            if not self.state[n].homed:
                raise MotionError(f"axis '{n}' is not homed")
        self._cancel_motion()
        self.set_play(speed, loop)
        self.playback = {"name": rec["name"], "phase": "positioning", "progress": 0.0,
                         "loop": self.play_loop, "pass": 1,
                         "duration": rec.get("duration", 0)}
        self._play_task = asyncio.get_running_loop().create_task(self._play_run(rec, axes))

    async def _play_run(self, rec: dict, axes: List[str]) -> None:
        pts = [{"t": p["t"], "pos": {n: p["pos"][n] for n in axes}} for p in rec["points"]]
        vmax = {n: self.cfg.axes[n].max_velocity for n in axes}
        state = self.playback
        try:
            while True:
                state["phase"] = "positioning"
                await self.goto(pts[0]["pos"], wait=True)
                state["phase"] = "playing"
                traj = PlaybackTrajectory(pts, vmax, lambda: self.play_speed)
                await self._start_traj(traj, "playback")
                if not self.play_loop:
                    break
                state["pass"] += 1
            log.info("replay of '%s' finished", rec["name"])
        except MotionError as e:
            log.info("replay of '%s' stopped: %s", rec["name"], e)
        except asyncio.CancelledError:
            pass
        finally:
            if self.playback is state:
                self.playback = None
                self._play_task = None

    async def play_stop(self) -> None:
        if self.playback is None:
            return
        names = list(self._traj.axes) if self._traj is not None else None
        self._cancel_motion()
        if names:
            await self.stop(names)

    # ================================================================ race tracking
    def lap_mark(self) -> int:
        n = self.recorder.lap_mark()
        st = self.recorder.state()
        if st.get("last_lap"):
            log.info("lap mark %d (lap %d: %.2f s)", n, n - 1, st["last_lap"])
        else:
            log.info("lap mark %d (start line)", n)
        return n

    def build_track(self, source: str, exclude=(), name: str = "") -> dict:
        summary = self.recorder.build_track(source, tuple(int(x) for x in exclude), name)
        log.info("track '%s' built: lap time %.2f s, %d/%d laps used", summary["name"],
                 summary["lap_time"], sum(1 for s in summary["laps"] if s["used"]),
                 len(summary["laps"]))
        return summary

    def _track_speed(self) -> float:
        tr = self.tracking
        if tr["target_lap"] and tr["lap_time"]:
            return tr["lap_time"] / tr["target_lap"]
        return tr["speed"]

    def track_set(self, speed: Optional[float] = None, target_lap: Optional[float] = None,
                  auto_rearm: Optional[bool] = None, auto_adjust: Optional[bool] = None,
                  adjust_tolerance: Optional[float] = None) -> None:
        """speed: replay factor. target_lap: expected lap time in seconds
        (overrides speed; 0 clears it). auto_rearm: return to the start point
        after each lap and wait for the next GO. auto_adjust: after each GO,
        move the lap time halfway to the measured GO-to-GO time, unless it
        differs by more than adjust_tolerance (fraction, e.g. 0.3 = 30 %)."""
        tr = self.tracking
        if speed is not None:
            tr["speed"] = clamp(float(speed), 0.1, 4.0)
        if target_lap is not None:
            tr["target_lap"] = float(target_lap) if float(target_lap) > 0 else None
        if auto_rearm is not None:
            tr["auto_rearm"] = bool(auto_rearm)
        if auto_adjust is not None:
            tr["auto_adjust"] = bool(auto_adjust)
            self._last_go = None          # measure from the next GO
        if adjust_tolerance is not None:
            tr["adjust_tolerance"] = clamp(float(adjust_tolerance), 0.02, 1.0)

    def current_lap_time(self) -> Optional[float]:
        tr = self.tracking
        return tr["lap_time"] / self._track_speed() if tr["lap_time"] else None

    async def track_arm(self, name: str) -> None:
        """Move to the start point of a learned lap and wait for GO."""
        self._check_ready()
        rec = self.recorder.load(name)
        axes = [n for n in rec["points"][0]["pos"]
                if n in self.state and self.axis_active[n]]
        if not axes:
            raise MotionError("track uses no configured axis")
        for n in axes:
            if not self.state[n].homed:
                raise MotionError(f"axis '{n}' is not homed")
        self._cancel_motion()
        self.tracking.update(phase="arming", track=rec["name"], runs=0, last_interval=None,
                             adjust_note="", lap_time=rec.get("lap_time") or rec.get("duration"))
        self._last_go = None
        self._go = asyncio.Event()
        self._track_task = asyncio.get_running_loop().create_task(self._track_run(rec, axes))

    async def _race(self, fut: Optional[asyncio.Future]) -> bool:
        """Wait for `fut` (a trajectory) or a GO. True if GO came first."""
        go = asyncio.ensure_future(self._go.wait())
        waits = {go} if fut is None else {go, fut}
        done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        if go in done:
            self._go.clear()
            return True
        go.cancel()
        fut.result()                      # raises MotionError if interrupted (jog, stop)
        return False

    async def _track_run(self, rec: dict, axes: List[str]) -> None:
        pts = [{"t": p["t"], "pos": {n: p["pos"][n] for n in axes}} for p in rec["points"]]
        vmax = {n: self.cfg.axes[n].max_velocity for n in axes}
        tr = self.tracking
        go = False
        try:
            while True:
                if not go:
                    if tr["phase"] != "done":            # back to the start point
                        tr["phase"] = "arming"
                        move = self._plan_move(pts[0]["pos"])
                        go = await self._race(self._start_traj(move, "tracking", replace=True))
                    if not go:
                        if tr["phase"] != "done":
                            tr["phase"] = "armed"
                            log.info("tracking armed on '%s', waiting for GO", rec["name"])
                        go = await self._race(None)
                # GO: (re)start the lap now, gliding from the current state onto it
                go = False
                tr["phase"] = "running"
                start_pos, start_vel = self._current_state(axes)
                lap = PlaybackTrajectory(pts, vmax, self._track_speed, natural_ends=True)
                traj = BlendIn(lap, start_pos, start_vel, vmax,
                               {n: self.cfg.axes[n].max_accel for n in axes})
                go = await self._race(self._start_traj(traj, "tracking", replace=True))
                tr["runs"] += 1
                if go:
                    log.info("GO during lap %d: next lap starts now", tr["runs"])
                    continue
                log.info("tracking lap %d done", tr["runs"])
                if not tr["auto_rearm"]:
                    tr["phase"] = "done"                  # hold; a GO still starts a lap
        except MotionError as e:
            log.info("tracking stopped: %s", e)
            tr["phase"] = "idle"
        except asyncio.CancelledError:
            pass

    def track_go(self, interval=_UNSET) -> None:
        """Timing signal: start the lap now. During a lap (or while returning
        to the start) the current lap is dropped and the new one starts.
        interval: lap time measured by the caller (car_lap: per car); by
        default the time since the previous GO."""
        tr = self.tracking
        if self._go is None or tr["phase"] == "idle" or self._track_task is None:
            raise MotionError("tracking is not armed: select a track and press ARM")
        loop_now = asyncio.get_running_loop().time()
        if interval is _UNSET:
            interval = loop_now - self._last_go if self._last_go is not None else None
        if interval is not None:
            tr["last_interval"] = round(interval, 3)
            if tr["auto_adjust"]:
                self._auto_adjust(interval)
        self._last_go = loop_now
        tr["phase"] = "running"                  # visible at once, before the task wakes
        self._go.set()
        log.info("GO (lap time %.2f s, x%.2f)", self.current_lap_time() or 0,
                 self._track_speed())

    def _auto_adjust(self, interval: float) -> None:
        tr = self.tracking
        cur = self.current_lap_time()
        if not cur:
            return
        dev = interval / cur - 1.0
        if abs(dev) > tr["adjust_tolerance"]:
            tr["adjust_note"] = (f"lap {interval:.2f} s ignored "
                                 f"({dev * 100:+.0f} % vs {cur:.2f} s)")
            log.info("auto adjust: %s", tr["adjust_note"])
            return
        new = (cur + interval) / 2.0
        tr["target_lap"] = round(new, 3)
        tr["adjust_note"] = f"lap {interval:.2f} s: next lap {cur:.2f} -> {new:.2f} s"
        log.info("auto adjust: %s", tr["adjust_note"])

    # ================================================================ cars
    @staticmethod
    def _new_car(name: str = "") -> dict:
        return {"name": name, "laps": 0, "last_lap": None, "best_lap": None,
                "expected": None, "crossings": 0}

    @staticmethod
    def _car_number(car) -> int:
        try:
            n = int(car)
        except (TypeError, ValueError):
            raise MotionError(f"car must be a number 1..{CAR_NUMBER_MAX}") from None
        if n != car and str(n) != str(car).strip():
            raise MotionError(f"car must be a number 1..{CAR_NUMBER_MAX}")
        if not 1 <= n <= CAR_NUMBER_MAX:
            raise MotionError(f"car must be 1..{CAR_NUMBER_MAX}")
        return n

    def _car(self, car) -> int:
        """Car number, added to the list the first time it is seen."""
        n = self._car_number(car)
        if n not in self.cars:
            if len(self.cars) >= CARS:
                raise MotionError(f"car {n} is not in the list (full: {CARS} cars; "
                                  "send a new list or flush it)")
            self.cars[n] = self._new_car()
            log.info("car %d added to the list", n)
            self._save_settings()
        return n

    def cars_set(self, cars) -> List[int]:
        """New car list from the timing software: numbers, or {"car", "name"}.
        Cars still in the list keep their lap times; the others are dropped."""
        if not isinstance(cars, list):
            raise MotionError("cars must be a list")
        new: Dict[int, dict] = {}
        for item in cars:
            num, name = (item.get("car"), item.get("name")) if isinstance(item, dict) else (item, None)
            n = self._car_number(num)
            if n in new:
                continue
            c = self.cars.get(n) or self._new_car()
            if name is not None:
                c["name"] = str(name).strip()[:24]
            new[n] = c
        if len(new) > CARS:
            raise MotionError(f"at most {CARS} cars")
        self.cars = new
        for d in (self._car_cross, self._car_pos, self._car_seq):
            for n in [n for n in d if n not in new]:
                del d[n]
        if self.current_car not in new:
            self.current_car = None
        self._save_settings()
        log.info("car list: %s", ", ".join(map(str, new)) or "empty")
        return list(new)

    def cars_flush(self) -> None:
        """Forget every car (numbers, names, lap times)."""
        self.cars.clear()
        self._car_cross.clear()
        self._car_pos.clear()
        self._car_seq.clear()
        self.current_car = None
        self._save_settings()
        log.info("car list flushed")

    def standings(self) -> List[int]:
        """Cars seen at the line, leader first. With positions sent by the
        timing software: by position (the latest report wins a tie). Else:
        the most crossings since the reset, then the earliest to cross."""
        seen = [n for n in self.cars if n in self._car_seq]
        if self._car_pos:
            key = lambda n: (self._car_pos.get(n, 10 ** 6), -self._car_seq[n])   # noqa: E731
        else:
            key = lambda n: (-self.cars[n]["crossings"], self._car_seq[n])      # noqa: E731
        return sorted(seen, key=key)

    def leader(self) -> Optional[int]:
        s = self.standings()
        return s[0] if s else None

    async def set_follow_leader(self, on: bool) -> dict:
        """Follow the leading car, whatever its number: when another car takes
        the lead (seen when it crosses the line first), the head switches to
        it at that crossing."""
        self.follow_leader = bool(on)
        lead = self.leader()
        state = None
        if self.follow_leader and lead is not None and lead != self.current_car:
            state = await self.await_car(lead, _from_leader=True)
        log.info("follow the leader: %s", "on" if self.follow_leader else "off")
        return {"follow_leader": self.follow_leader, "leader": lead, "state": state}

    def _car_label(self, n: Optional[int]) -> str:
        if n is None:
            return "any car"
        return f"car {n}" + (f" ({self.cars[n]['name']})" if self.cars[n]["name"] else "")

    def set_car_name(self, car, name: str) -> None:
        self.cars[self._car(car)]["name"] = str(name or "").strip()[:24]
        self._save_settings()

    def cars_reset(self) -> None:
        """New race: forget every lap time and crossing count (the list and
        names are kept)."""
        for c in self.cars.values():
            c.update(laps=0, last_lap=None, best_lap=None, expected=None, crossings=0)
        self._car_cross.clear()
        self._car_pos.clear()
        self._car_seq.clear()
        log.info("car lap times cleared")

    async def await_car(self, car, track: Optional[str] = None,
                        _from_leader: bool = False) -> str:
        """Follow another car: the head goes to the start line and waits for
        that car's next crossing (a "lap" event with its number).

        Learning a track: the lap in progress is dropped, the head goes to
        where it looked at the line, and the next crossing starts a new lap.
        Tracking (armed or running, or `track` given): re-arm on the track;
        GO now only comes from this car. Otherwise the car is just selected.
        Choosing a car turns "follow the leader" off.
        Returns "learning", "tracking" or "selected"."""
        c = self._car(car)
        self.current_car = c
        if not _from_leader:
            self.follow_leader = False
        rec, tr = self.recorder, self.tracking
        if rec.active and rec.mode == "laps":
            rec.lap_break()
            pos = {n: v for n, v in rec.line_position().items()
                   if n in self.state and self.state[n].homed and self.axis_active[n]}
            log.info("learning: now following %s, waiting at the line", self._car_label(c))
            if pos:
                await self.goto(pos)
            return "learning"
        name = track or (tr["track"] if tr["phase"] != "idle" else None)
        if name:
            exp = self.cars[c]["expected"]
            if tr["auto_adjust"] and exp:
                tr["target_lap"] = exp
            await self.track_arm(name)
            log.info("tracking: now following %s", self._car_label(c))
            return "tracking"
        log.info("now following %s", self._car_label(c))
        return "selected"

    def car_lap(self, car=None, position=None) -> dict:
        """A car crosses the start / finish line (timing software).

        Every car's lap time is kept. The car followed (see await_car; any
        car while none is selected) also drives the head: a lap mark while
        learning, a GO while tracking. position: its race position, if the
        timing software knows it (else the leader comes from the crossings).
        Following the leader: a car that crosses the line as the new leader
        becomes the car followed at once (it is on the line right now)."""
        if position is not None:
            try:
                position = int(position)
                assert position >= 1
            except (TypeError, ValueError, AssertionError):
                raise MotionError("position must be 1 or more") from None
        try:
            c = self.current_car if car is None else self._car(car)
        except MotionError:
            if car is not None and len(self.cars) >= CARS:
                return {"car": car, "action": "unknown"}     # list full: not tracked
            raise
        now = asyncio.get_running_loop().time()
        interval = None
        if c is not None:
            prev = self._car_cross.get(c)
            if prev is not None and now - prev < _track.MIN_LAP_S:
                return {"car": c, "action": "ignored"}       # repeated signal
            self._car_cross[c] = now
            self._seq += 1
            self._car_seq[c] = self._seq
            if position is not None:
                self._car_pos[c] = position
            info = self.cars[c]
            info["crossings"] += 1
            if prev is not None:
                lap = now - prev
                ref = info["expected"] or info["last_lap"]
                if ref is None and c == self.current_car:
                    ref = self.current_lap_time()
                if ref is None or lap <= 1.6 * ref:          # else: laps missed meanwhile
                    interval = lap
                    info["laps"] += 1
                    info["last_lap"] = round(lap, 3)
                    if info["best_lap"] is None or lap < info["best_lap"]:
                        info["best_lap"] = round(lap, 3)
                    exp = info["expected"]
                    tol = self.tracking["adjust_tolerance"]
                    if exp is None:
                        info["expected"] = round(lap, 3)
                    elif abs(lap / exp - 1.0) <= tol:
                        info["expected"] = round((exp + lap) / 2.0, 3)
        switched = False
        if (self.follow_leader and c is not None and c != self.current_car
                and self.leader() == c):
            log.info("%s takes the lead: following it", self._car_label(c))
            self.current_car, switched = c, True
        if self.current_car is not None and c != self.current_car:
            return {"car": c, "action": "none"}
        action = "none"
        rec = self.recorder
        if rec.active and rec.mode == "laps":
            if switched:
                rec.lap_break()          # the old leader's lap in progress is dropped
            self.lap_mark()
            action = "mark"
        elif self.tracking["phase"] != "idle" and self._track_task is not None:
            exp = self.cars[c]["expected"] if c is not None else None
            if switched and self.tracking["auto_adjust"] and exp:
                self.tracking["target_lap"] = exp
            self.track_go(interval)
            if c is not None and self.tracking["auto_adjust"] and self.tracking["target_lap"]:
                self.cars[c]["expected"] = self.tracking["target_lap"]
            action = "go"
        return {"car": c, "action": action, **({"switched": True} if switched else {})}

    def cars_status(self) -> dict:
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            now = None
        waiting = bool((self.recorder.active and self.recorder.mode == "laps"
                        and self.recorder.waiting)
                       or self.tracking["phase"] in ("arming", "armed"))
        rank = {n: i + 1 for i, n in enumerate(self.standings())}
        return {"current": self.current_car, "waiting": waiting, "max": CARS,
                "follow_leader": self.follow_leader, "leader": next(iter(rank), None),
                "list": [{"car": n, **c, "position": rank.get(n),
                          "since": (round(now - self._car_cross[n], 1)
                                    if now is not None and n in self._car_cross else None)}
                         for n, c in self.cars.items()]}

    async def track_abort(self) -> None:
        names = list(self._traj.axes) if self._traj is not None else None
        self._cancel_motion()
        if names:
            await self.stop(names)

    # ================================================================ status
    def status(self) -> dict:
        axes = {}
        for ax in self.axes:
            st = self.state[ax.name]
            f = st.flags
            axes[ax.name] = {
                "pos": round(ax.to_units(st.pos), 4),
                "vel": round(st.vel / ax.steps_per_unit, 4),
                "homed": st.homed,
                "homing": ax.name in self._homing,
                "steps": st.pos,
                "raw": ({k: v for k, v in self._raw[ax.name].items() if k != "task"}
                        if ax.name in self._raw else None),
                "halted": bool(f & P.ST_HALTED),
                "servo": ax.servo,
                "pulse": round(st.pos / P.SERVO_TICKS_PER_US, 1) if ax.servo else None,
                "available": ax.name not in self.servo_unsupported,
                "enabled": bool(f & P.ST_ENABLED),
                "moving": bool(f & P.ST_MOVING),
                "endstop": bool(f & P.ST_ENDSTOP),
                "at_limit": bool(f & P.ST_AT_LIMIT) or st.braking,
                "min": ax.position_min,
                "max": ax.position_max,
                "units": ax.units,
                "active": self.axis_active[ax.name],
            }
        playback = None
        if self.playback is not None:
            playback = dict(self.playback)
            if self._traj_kind == "playback" and self._traj is not None:
                playback["progress"] = round(self._traj.progress, 3)
        return {
            "connected": self.connected,
            "ready": self.ready,
            "estop": self.estopped,
            "settings": self.settings(),
            "lock": ({"owner": self.lock_owner, "label": self.lock_label}
                     if self.lock_owner else None),
            "sources": self.active_sources(),
            "motion": "move" if self._traj_kind == "park" else self._traj_kind,
            "shutdown": self.shutdown_state,
            "playback": playback,
            "recording": self.recorder.state(),
            "limits": self.limits_status(),
            "level": self.level_status(),
            "lens": self.lens_status(),
            "cars": self.cars_status(),
            "tracking": {**self.tracking, "speed_eff": round(self._track_speed(), 3),
                         "current_lap": (round(self.current_lap_time(), 3)
                                         if self.current_lap_time() else None),
                         "progress": round(self._traj.progress, 3)
                         if self._traj_kind == "tracking" and self._traj is not None else None},
            "error": self.last_error,
            "config_error": getattr(self, "config_error", ""),
            "mcu_warning": self.mcu_warning,
            "firmware": self.fw_version,
            "axes": axes,
        }
