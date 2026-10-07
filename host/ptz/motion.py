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
from .mcu import McuError, McuLink
from .presets import PresetStore
from .recorder import Recorder
from .trajectory import (MoveTrajectory, PlaybackTrajectory, VelocityShaper, braking_speed,
                         clamp)

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
        self.lock_owner: Optional[str] = None
        self.lock_label = ""
        self.sources: Dict[Tuple[str, str], float] = {}   # (kind, ip) -> last seen
        self._refused_log = 0.0
        self.play_speed = 1.0                 # replay speed factor
        self.play_loop = False
        self._settings_path = os.path.join(cfg.server.state_dir, "settings.json")
        self._load_settings()
        self.recorder = Recorder(os.path.join(cfg.server.state_dir, "recordings"),
                                 self._homed_positions, time.monotonic)
        self.playback: Optional[dict] = None
        self._play_task: Optional[asyncio.Task] = None
        self._traj = None                     # MoveTrajectory | PlaybackTrajectory
        self._traj_kind = ""
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
        self._waiters: List[tuple] = []       # (axis_idx, types, future)
        self._tasks: List[asyncio.Task] = []
        link.on_status = self._on_status
        link.on_event = self._on_event

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
        if pong["protocol_version"] != P.PROTOCOL_VERSION:
            raise MotionError(f"firmware protocol v{pong['protocol_version']}, "
                              f"host expects v{P.PROTOCOL_VERSION}: reflash the MCU")
        await self.link.request("RESET")
        await self.link.request("SET_STATUS_RATE", rate_hz=c.mcu.status_rate)
        await self.link.request("SET_WATCHDOG",
                                timeout_ms=int(c.mcu.watchdog_timeout * 1000))
        # Axes first, motors re-energized right away: RESET released the drivers
        # and the tilt must not stay unpowered while the TMC registers are
        # written (the drivers keep their previous register values meanwhile).
        for ax in self.axes:
            flags = 0
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
        for k in ("smoothing", "play_speed"):
            if isinstance(data.get(k), num):
                setattr(self, k, float(data[k]))

    def _save_settings(self) -> None:
        data = {"advanced": self.advanced, "speed_all": self.speed_all,
                "accel_all": self.accel_all, "speed_axis": self.speed_axis,
                "accel_axis": self.accel_axis, "smoothing": self.smoothing,
                "play_speed": self.play_speed}
        try:
            os.makedirs(os.path.dirname(self._settings_path), exist_ok=True)
            with open(self._settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
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

    def jog(self, values: Dict[str, float]) -> List[str]:
        """Normalized velocity command (-1..1) per axis. Must be refreshed
        by the caller at least every motion.jog_timeout seconds.

        A non-zero jog on an axis that is running a move or a replay takes
        over (manual override). Zero values never interrupt a move.
        Returns the axes refused because they are not homed."""
        if not self.ready or self.estopped:
            return []
        now = asyncio.get_running_loop().time()
        shaped = {n: self._shape(v) for n, v in values.items()
                  if n in self.state and n not in self._homing}
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

    def _stream_tick(self, now: float, dt: float) -> None:
        mask, vels = 0, [0.0] * P.MAX_AXES
        traj = self._traj
        if traj is not None:
            traj.advance(dt)
            samples = traj.sample()
            self._des_hist.append((now, {n: p for n, (p, _) in samples.items()}))
            ref = None
            if now - self._last_status < 0.1:
                ref = self._desired_at(self._last_status - FEEDBACK_LATENCY)
            for name, (p, v) in samples.items():
                ax = self.cfg.axes[name]
                if ref is not None and name in ref:
                    corr = KP * (ref[name] - self.position(name))
                    v += clamp(corr, -0.1 * ax.max_velocity, 0.1 * ax.max_velocity)
                v = clamp(v, -ax.max_velocity, ax.max_velocity)
                self._cmd_vel[name] = v
                mask |= 1 << ax.index
                vels[ax.index] = v * ax.steps_per_unit
            if traj.done:
                self._finish_traj(traj)

        timeout = self.cfg.motion.jog_timeout
        smooth = self._smooth_time()
        for ax in self.axes:
            name = ax.name
            if (traj is not None and name in traj.axes) or name in self._homing:
                continue
            st, sh = self.state[name], self._shapers[name]
            if not st.jog_active and sh.v == 0.0:
                continue
            if st.jog_active and now - st.jog_time > timeout:
                st.jog_active, st.jog_value = False, 0.0
            amax = self.accel_of(name)
            jerk = amax / smooth if smooth > 1e-3 else math.inf
            target = st.jog_value * self.speed_of(name)
            st.braking = False
            if st.homed:                              # ease into the soft limits
                pos = self.position(name)
                up = braking_speed(ax.position_max - pos, amax, smooth)
                dn = braking_speed(pos - ax.position_min, amax, smooth)
                if target > up or target < -dn:
                    target = clamp(target, -dn, up)
                    st.braking = True
            v = sh.step(target, amax, jerk, dt)
            self._cmd_vel[name] = v
            mask |= 1 << ax.index
            vels[ax.index] = v * ax.steps_per_unit
        if mask:
            self.link.send("SET_VELOCITY", axis_mask=mask, v0=vels[0], v1=vels[1],
                           v2=vels[2], v3=vels[3])

    # ================================================================ trajectories
    def _start_traj(self, traj, kind: str) -> asyncio.Future:
        self._cancel_traj()
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
                await self.link.request("MOVE_TO", axis=ax.index, target=ax.to_steps(target),
                                        max_vel=ax.max_velocity * spu * 0.5,
                                        accel=ax.max_accel * spu)
            await asyncio.wait_for(asyncio.gather(*waits), 3.0)
            if fut is not None and not fut.done():
                fut.set_result(True)
        except Exception as e:  # noqa: BLE001
            if fut is not None and not fut.done():
                fut.set_exception(MotionError(f"move did not complete: {e}"))

    def _cancel_traj(self, handover: bool = False) -> None:
        traj, fut = self._traj, self._traj_future
        self._traj, self._traj_kind, self._traj_future = None, "", None
        if traj is not None:
            for n in traj.axes:      # continue smoothly from the streamed speed
                self._shapers[n].reset(self._cmd_vel[n] if handover else 0.0)
        if fut is not None and not fut.done():
            fut.set_exception(MotionError("move interrupted"))

    def _cancel_motion(self, handover: bool = False) -> None:
        """Stop any running move and replay (jog sources are kept)."""
        if self._play_task is not None and not self._play_task.done():
            self._play_task.cancel()
        self._play_task = None
        self.playback = None
        self._cancel_traj(handover)

    # ================================================================ moves
    async def goto(self, targets: Dict[str, float], speed: float = 1.0,
                   wait: bool = False) -> None:
        """Absolute move in user units. All axes arrive at the same time,
        with the current acceleration and ease in/out settings."""
        self._check_ready()
        k = max(0.01, min(1.0, speed))
        start, final, vmax, amax = {}, {}, {}, {}
        for name, target in targets.items():
            ax = self.axis(name)
            if not self.state[name].homed:
                raise MotionError(f"axis '{name}' is not homed")
            if name in self._homing:
                raise MotionError(f"axis '{name}' is homing")
            final[name] = max(ax.position_min, min(ax.position_max, float(target)))
            start[name] = self.position(name)
            vmax[name] = self.speed_of(name) * k
            amax[name] = self.accel_of(name)
        if not final:
            return
        fut = self._start_traj(MoveTrajectory(start, final, vmax, amax, self._smooth_time()),
                               "move")
        if wait:
            await fut
        else:
            fut.add_done_callback(lambda f: f.cancelled() or f.exception())

    async def move_relative(self, deltas: Dict[str, float], speed: float = 1.0,
                            wait: bool = False) -> None:
        await self.goto({n: self.position(n) + d for n, d in deltas.items()},
                        speed=speed, wait=wait)

    async def stop(self, names: Optional[Iterable[str]] = None) -> None:
        names = list(names) if names else [a.name for a in self.axes]
        self._cancel_motion()
        self._cancel_jog(names)
        mask = 0
        for n in names:
            mask |= 1 << self.axis(n).index
        await self.link.request("STOP", axis_mask=mask)

    async def estop(self) -> None:
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
        mask = 0
        for n in names:
            mask |= 1 << self.axis(n).index
            if not on:
                self.state[n].homed = False     # may move freely while unpowered
                self._cancel_jog([n])
        await self.link.request("ENABLE", axis_mask=mask, enable_mask=mask if on else 0)

    # ================================================================ homing
    async def home(self, names: Optional[Iterable[str]] = None) -> None:
        """Home the given axes, or every axis with home_with_all = True."""
        self.last_error = ""
        names = list(names) if names else [a.name for a in self.axes if a.home_with_all]
        self._check_ready()
        if not names:
            raise MotionError("no axis to home")
        await asyncio.gather(*(self._home_axis(self.axis(n)) for n in names))

    async def _home_axis(self, ax: AxisConfig) -> None:
        st = self.state[ax.name]
        if ax.name in self._homing:
            raise MotionError(f"axis '{ax.name}' already homing")
        self._homing.add(ax.name)
        self._cancel_jog([ax.name])
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
            await self.link.request("SET_LIMITS", axis=ax.index,
                                    min=ax.to_steps(ax.position_min),
                                    max=ax.to_steps(ax.position_max), enabled=1)
            st.homed = True
            log.info("%s homed", ax.name)
        finally:
            self._homing.discard(ax.name)
        target = ax.park_position
        if target is None and not ax.position_min <= ax.position_endstop <= ax.position_max:
            target = min(max(ax.position_endstop, ax.position_min), ax.position_max)
        if target is not None:
            await self.goto({ax.name: target}, wait=True)

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
        axes = [n for n in rec["points"][0]["pos"] if n in self.state]
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

    # ================================================================ status
    def status(self) -> dict:
        axes = {}
        for ax in self.axes:
            st = self.state[ax.name]
            f = st.flags
            axes[ax.name] = {
                "pos": round(ax.to_units(st.pos), 4),
                "vel": round(ax.to_units(st.vel), 4),
                "homed": st.homed,
                "homing": ax.name in self._homing,
                "enabled": bool(f & P.ST_ENABLED),
                "moving": bool(f & P.ST_MOVING),
                "endstop": bool(f & P.ST_ENDSTOP),
                "at_limit": bool(f & P.ST_AT_LIMIT) or st.braking,
                "min": ax.position_min,
                "max": ax.position_max,
                "units": ax.units,
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
            "motion": self._traj_kind,
            "playback": playback,
            "recording": self.recorder.state(),
            "error": self.last_error,
            "axes": axes,
        }
