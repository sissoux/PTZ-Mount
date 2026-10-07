"""Motion controller: the brain of the PTZ head, running on the Raspberry Pi.

Responsibilities
  * configure the MCU (pins, limits, TMC2209 registers) from ptz.cfg
  * convert user units <-> microsteps
  * jog: normalized joystick values -> velocity stream (with deadband/expo)
  * goto: synchronized multi-axis point-to-point moves
  * homing sequence (fast approach, retract, slow approach, set position)
  * presets, enable/disable, stop / emergency stop
  * aggregate status for the API layer

All real-time work (ramps, step pulses, endstop checks, soft limits) is done
by the RP2040. Nothing here is time critical beyond ~10 ms.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

from . import protocol as P
from . import tmc2209
from .config import AxisConfig, PtzConfig
from .mcu import McuError, McuLink
from .presets import PresetStore

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


def trapezoid_time(dist: float, vmax: float, accel: float) -> float:
    dist = abs(dist)
    if dist == 0 or vmax <= 0 or accel <= 0:
        return 0.0
    if dist >= vmax * vmax / accel:
        return dist / vmax + vmax / accel
    return 2.0 * math.sqrt(dist / accel)


class MotionController:
    def __init__(self, cfg: PtzConfig, link: McuLink):
        self.cfg = cfg
        self.link = link
        self.axes: List[AxisConfig] = sorted(cfg.axes.values(), key=lambda a: a.index)
        self.state: Dict[str, AxisState] = {a.name: AxisState() for a in self.axes}
        self.presets = PresetStore(os.path.join(cfg.server.state_dir, "presets.json"))
        self.speed = 1.0                      # global speed factor 0..1
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
        self._tasks.append(loop.create_task(self._jog_loop()))

    async def close(self) -> None:
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
        if c.tmc_uart:
            await self.link.request("CONFIG_TMC_UART", rx_pin=c.tmc_uart.rx_pin.gpio,
                                    tx_pin=c.tmc_uart.tx_pin.gpio, baud=c.tmc_uart.baud)
            for ax in self.axes:
                await self._configure_tmc(ax)
        for ax in self.axes:
            flags = 0
            flags |= P.AXF_DIR_INVERT if ax.dir_pin.invert else 0
            flags |= P.AXF_ENABLE_INVERT if ax.enable_pin.invert else 0
            flags |= P.AXF_ENDSTOP_INVERT if ax.endstop_pin.invert else 0
            flags |= P.AXF_ENDSTOP_PULLUP if ax.endstop_pin.pullup else 0
            es_dir = (1 if ax.homing_positive_dir else -1) if ax.has_endstop else 0
            spu = ax.steps_per_unit
            await self.link.request(
                "CONFIG_AXIS", axis=ax.index, step_pin=ax.step_pin.gpio,
                dir_pin=ax.dir_pin.gpio, enable_pin=ax.enable_pin.gpio,
                endstop_pin=ax.endstop_pin.gpio, flags=flags, endstop_dir=es_dir,
                max_vel=ax.max_velocity * spu, max_accel=ax.max_accel * spu,
                vel_accel=ax.jog_accel * spu)
            self.state[ax.name].homed = False
        self.ready = True
        self.last_error = ""
        log.info("MCU configured: %d axes (%s)", len(self.axes),
                 ", ".join(a.name for a in self.axes))
        if c.motion.enable_on_start:
            await self.enable(True)

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
        self.sys_flags = msg["sys_flags"]
        for ax, (pos, vel, flags) in zip(self.axes, P.status_axes(msg)):
            st = self.state[ax.name]
            st.pos, st.vel, st.flags = pos, vel, flags

    def _on_event(self, msg: P.Message) -> None:
        etype, axis = msg["type"], msg["axis"]
        if etype == P.EV_BOOT:
            if self.ready:
                log.warning("MCU rebooted, reconfiguring (all axes unhomed)")
            self.ready = False
            for st in self.state.values():
                st.homed = False
            self._fail_waiters(MotionError("MCU rebooted"))
            return
        if etype == P.EV_ENDSTOP_HIT:
            log.warning("axis %d hit its endstop outside homing", axis)
        elif etype == P.EV_WATCHDOG:
            log.info("MCU watchdog: jog stopped (no command received in time)")
        for w in list(self._waiters):
            w_axis, types, fut = w
            if w_axis == axis and etype in types and not fut.done():
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

    def jog(self, values: Dict[str, float]) -> None:
        """Normalized velocity command (-1..1) per axis. Must be refreshed
        by the caller at least every motion.jog_timeout seconds."""
        if not self.ready or self.estopped:
            return
        now = asyncio.get_running_loop().time()
        for name, v in values.items():
            if name not in self.state or name in self._homing:
                continue
            st = self.state[name]
            st.jog_value, st.jog_time, st.jog_active = self._shape(v), now, True
        self._send_jog(now)

    def _send_jog(self, now: float) -> None:
        mask, vels = 0, [0.0] * P.MAX_AXES
        timeout = self.cfg.motion.jog_timeout
        for ax in self.axes:
            st = self.state[ax.name]
            if not st.jog_active:
                continue
            mask |= 1 << ax.index
            if now - st.jog_time > timeout:
                st.jog_active = False         # send one last zero
                st.jog_value = 0.0
            vels[ax.index] = st.jog_value * ax.jog_velocity * self.speed * ax.steps_per_unit
        if mask:
            self.link.send("SET_VELOCITY", axis_mask=mask, v0=vels[0], v1=vels[1],
                           v2=vels[2], v3=vels[3])

    async def _jog_loop(self) -> None:
        """Re-send the jog vector so the MCU watchdog stays fed while a
        client is actively jogging, and stale jog sources get zeroed."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(0.05)
            if self.ready:
                self._send_jog(loop.time())

    def _cancel_jog(self, names: Iterable[str]) -> None:
        for n in names:
            st = self.state[n]
            st.jog_active, st.jog_value = False, 0.0

    # ================================================================ moves
    async def goto(self, targets: Dict[str, float], speed: float = 1.0,
                   wait: bool = False) -> None:
        """Absolute move in user units. All axes arrive at the same time."""
        self._check_ready()
        speed = max(0.01, min(1.0, speed)) * self.speed
        plan = []
        for name, target in targets.items():
            ax = self.axis(name)
            st = self.state[name]
            if not st.homed:
                raise MotionError(f"axis '{name}' is not homed")
            if name in self._homing:
                raise MotionError(f"axis '{name}' is homing")
            target = max(ax.position_min, min(ax.position_max, float(target)))
            d = ax.to_steps(target) - st.pos
            vmax = ax.max_velocity * ax.steps_per_unit * speed
            acc = ax.max_accel * ax.steps_per_unit
            plan.append([ax, ax.to_steps(target), d, vmax, acc])
        if not plan:
            return
        # synchronize: scale every profile onto the slowest one
        lead = max(plan, key=lambda p: trapezoid_time(p[2], p[3], p[4]))
        if lead[2] != 0:
            for p in plan:
                if p is lead:
                    continue
                k = abs(p[2] / lead[2])
                p[3] = min(p[3], lead[3] * k) or p[3]
                p[4] = min(p[4], lead[4] * k) or p[4]
        self._cancel_jog(p[0].name for p in plan)
        futs = []
        for ax, target, d, vmax, acc in plan:
            if wait:
                futs.append(self._expect(ax.index, P.EV_MOVE_DONE))
            await self.link.request("MOVE_TO", axis=ax.index, target=target,
                                    max_vel=vmax, accel=acc)
        if futs:
            await asyncio.gather(*futs)

    async def move_relative(self, deltas: Dict[str, float], speed: float = 1.0,
                            wait: bool = False) -> None:
        await self.goto({n: self.position(n) + d for n, d in deltas.items()},
                        speed=speed, wait=wait)

    async def stop(self, names: Optional[Iterable[str]] = None) -> None:
        names = list(names) if names else [a.name for a in self.axes]
        self._cancel_jog(names)
        mask = 0
        for n in names:
            mask |= 1 << self.axis(n).index
        await self.link.request("STOP", axis_mask=mask)

    async def estop(self) -> None:
        self._cancel_jog(self.state.keys())
        await self.link.request("ESTOP", retries=3)
        self._fail_waiters(MotionError("emergency stop"))
        log.warning("EMERGENCY STOP")

    async def clear_estop(self) -> None:
        await self.link.request("CLEAR_ESTOP")

    async def enable(self, on: bool, names: Optional[Iterable[str]] = None) -> None:
        names = list(names) if names else [a.name for a in self.axes]
        mask = 0
        for n in names:
            mask |= 1 << self.axis(n).index
            if not on:
                self.state[n].homed = False     # may move freely while unpowered
        await self.link.request("ENABLE", axis_mask=mask, enable_mask=mask if on else 0)

    # ================================================================ homing
    async def home(self, names: Optional[Iterable[str]] = None) -> None:
        names = list(names) if names else [a.name for a in self.axes]
        self._check_ready()
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
                await self.link.request("SET_POSITION", axis=ax.index,
                                        position=ax.to_steps(ax.position_endstop))
            else:
                d = 1 if ax.homing_positive_dir else -1
                travel = ax.to_steps((ax.position_max - ax.position_min) * 1.5
                                     + ax.homing_retract_dist)
                log.info("homing %s", ax.name)
                trig = await self._home_approach(ax, d * ax.homing_speed * spu, travel)
                retract = ax.to_steps(ax.homing_retract_dist)
                fut = self._expect(ax.index, P.EV_MOVE_DONE)
                await self.link.request("MOVE_TO", axis=ax.index, target=trig - d * retract,
                                        max_vel=ax.homing_speed * spu,
                                        accel=ax.max_accel * spu)
                await asyncio.wait_for(fut, 30)
                await self._home_approach(ax, d * ax.second_homing_speed * spu, retract * 3)
                await self.link.request("SET_POSITION", axis=ax.index,
                                        position=ax.to_steps(ax.position_endstop))
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
                "at_limit": bool(f & P.ST_AT_LIMIT),
                "min": ax.position_min,
                "max": ax.position_max,
            }
        return {
            "connected": self.connected,
            "ready": self.ready,
            "estop": self.estopped,
            "speed": self.speed,
            "error": self.last_error,
            "axes": axes,
        }
