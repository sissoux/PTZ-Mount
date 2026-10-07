"""Simulated MCU.

Speaks the exact same binary protocol as the RP2040 firmware and implements
the same control law (see firmware/src/stepper.c). Used to develop the host,
web UI and clients on a PC:  python -m ptz --sim

Each simulated endstop sits SIM_ENDSTOP_DISTANCE microsteps away from the
power-on position, on the side given by CONFIG_AXIS.endstop_dir (or, when the
guard is disabled, by the direction of the first HOME command). It behaves like
a cam: pressed for every position beyond it.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Callable, List, Optional

from . import protocol as P

SIM_ENDSTOP_DISTANCE = 1000
CONTROL_DT = 0.001
MAX_STEP_RATE = 20000.0

MODE_IDLE, MODE_VELOCITY, MODE_POSITION, MODE_HOMING = range(4)


class SimAxis:
    def __init__(self):
        self.configured = False
        self.enabled = False
        self.max_vel = 0.0
        self.max_accel = 0.0
        self.vel_accel = 0.0
        self.stopping = False   # STOP/watchdog: decelerate with max_accel
        self.endstop_dir = 0
        self.has_endstop = False
        self.es_side = -1
        self.pos = 0.0              # microsteps (float, integer in firmware)
        self.offset = 0.0           # physical = pos + offset
        self.v = 0.0
        self.v_cmd = 0.0
        self.mode = MODE_IDLE
        self.target = 0
        self.move_vmax = 0.0
        self.move_accel = 0.0
        self.home_vel = 0.0
        self.home_start = 0.0
        self.home_max_travel = 0
        self.lim_en = False
        self.lim_min = 0
        self.lim_max = 0
        self.at_limit = False
        self.halted = False
        self.guard_dir = 0

    def endstop_active(self) -> bool:
        if not self.has_endstop:
            return False
        phys = self.pos + self.offset
        es = self.es_side * SIM_ENDSTOP_DISTANCE
        return (phys - es) * self.es_side >= 0


class SimTransport:
    def __init__(self):
        self.axes = [SimAxis() for _ in range(P.MAX_AXES)]
        self.estop = False
        self.watchdog_ms = 300
        self.watchdog_expired = False
        self.last_feed = time.monotonic()
        self.status_rate = 50
        self.tmc_ready = False
        self._decoder = P.FrameDecoder()
        self._on_data: Optional[Callable[[bytes], None]] = None
        self._task: Optional[asyncio.Task] = None
        self._t0 = time.monotonic()
        self.tmc_regs = {}

    # ------------------------------------------------------------ transport API
    async def open(self, on_data):
        self._on_data = on_data
        self._task = asyncio.get_running_loop().create_task(self._run())
        self._emit("EVENT", type=P.EV_BOOT, axis=0, value=0)

    def write(self, data: bytes) -> None:
        for msg in self._decoder.feed(data):
            self._handle(msg)

    async def close(self):
        if self._task:
            self._task.cancel()

    # ------------------------------------------------------------ helpers
    def _emit(self, name, seq=0, **fields):
        frame = P.encode(name, seq, **fields)
        if self._on_data:
            asyncio.get_running_loop().call_soon(self._on_data, frame)

    def _ack(self, msg, status=P.ACK_OK):
        self._emit("ACK", msg.seq, acked_id=msg.id, status=status)

    def _axis(self, idx) -> Optional[SimAxis]:
        if 0 <= idx < P.MAX_AXES and self.axes[idx].configured:
            return self.axes[idx]
        return None

    # ------------------------------------------------------------ commands
    def _handle(self, m: P.Message):
        n, f = m.name, m.fields
        if n == "PING":
            self._emit("PONG", m.seq, protocol_version=P.PROTOCOL_VERSION,
                       max_axes=P.MAX_AXES, uptime_ms=self._ms())
        elif n == "RESET":
            self.axes = [SimAxis() for _ in range(P.MAX_AXES)]
            self.estop = False
            self._ack(m)
        elif n == "SET_STATUS_RATE":
            self.status_rate = max(1, f["rate_hz"])
            self._ack(m)
        elif n == "SET_WATCHDOG":
            self.watchdog_ms = f["timeout_ms"]
            self._ack(m)
        elif n == "CONFIG_AXIS":
            if f["axis"] >= P.MAX_AXES:
                return self._ack(m, 3)
            a = self.axes[f["axis"]]
            a.configured = True
            a.max_vel = min(f["max_vel"], MAX_STEP_RATE)
            a.max_accel = f["max_accel"]
            a.vel_accel = min(f["vel_accel"], f["max_accel"]) or f["max_accel"]
            a.endstop_dir = f["endstop_dir"]
            a.has_endstop = f["endstop_pin"] != P.PIN_NONE
            if a.endstop_dir:
                a.es_side = a.endstop_dir
            self._ack(m)
        elif n == "CONFIG_TMC_UART":
            self.tmc_ready = True
            self._ack(m)
        elif n == "TMC_WRITE":
            self.tmc_regs[(f["addr"], f["reg"])] = f["value"]
            cnt = self.tmc_regs.get((f["addr"], 0x02), 0)      # IFCNT
            self.tmc_regs[(f["addr"], 0x02)] = (cnt + 1) & 0xFF
            self._ack(m)
        elif n == "TMC_READ":
            v = self.tmc_regs.get((f["addr"], f["reg"]), 0)
            self._emit("TMC_VALUE", m.seq, addr=f["addr"], reg=f["reg"], value=v, ok=1)
        elif n == "SET_LIMITS":
            a = self._axis(f["axis"])
            if a is None:
                return self._ack(m, 4)
            a.lim_min, a.lim_max, a.lim_en = f["min"], f["max"], bool(f["enabled"])
            self._ack(m)
        elif n == "ENABLE":
            for i, a in enumerate(self.axes):
                if f["axis_mask"] & (1 << i):
                    a.enabled = bool(f["enable_mask"] & (1 << i))
                    if not a.enabled:
                        a.mode, a.v, a.v_cmd = MODE_IDLE, 0.0, 0.0
            self._ack(m)
        elif n == "SET_VELOCITY":
            self._feed()
            if self.estop:
                return
            for i, a in enumerate(self.axes):
                if f["axis_mask"] & (1 << i) and a.configured and a.enabled:
                    if a.mode != MODE_HOMING:
                        a.mode = MODE_VELOCITY
                        a.v_cmd = f[f"v{i}"]
                        a.stopping = False
        elif n == "KEEPALIVE":
            self._feed()
        elif n == "MOVE_TO":
            a = self._axis(f["axis"])
            if a is None or not a.enabled:
                return self._ack(m, 4)
            if self.estop:
                return self._ack(m, 5)
            a.mode, a.target = MODE_POSITION, f["target"]
            a.move_vmax = min(f["max_vel"], a.max_vel) or a.max_vel
            a.move_accel = min(f["accel"], a.max_accel) or a.max_accel
            self._ack(m)
        elif n == "STOP":
            for i, a in enumerate(self.axes):
                if f["axis_mask"] & (1 << i) and a.mode != MODE_IDLE:
                    a.mode, a.v_cmd, a.stopping = MODE_VELOCITY, 0.0, True
            self._ack(m)
        elif n == "ESTOP":
            self.estop = True
            for a in self.axes:
                a.mode, a.v, a.v_cmd = MODE_IDLE, 0.0, 0.0
            self._ack(m)
        elif n == "CLEAR_ESTOP":
            self.estop = False
            self._ack(m)
        elif n == "HOME":
            a = self._axis(f["axis"])
            if a is None or not a.enabled or not a.has_endstop:
                return self._ack(m, 4)
            if self.estop:
                return self._ack(m, 5)
            a.mode, a.home_vel = MODE_HOMING, f["velocity"]
            if not a.endstop_dir:
                a.es_side = 1 if a.home_vel > 0 else -1
            a.home_start, a.home_max_travel = a.pos, f["max_travel"]
            a.halted = False
            self._ack(m)
        elif n == "SET_POSITION":
            a = self._axis(f["axis"])
            if a is None:
                return self._ack(m, 4)
            phys = a.pos + a.offset
            a.pos = float(f["position"])
            a.offset = phys - a.pos
            self._ack(m)
        else:
            self._ack(m, 1)

    def _feed(self):
        self.last_feed = time.monotonic()
        self.watchdog_expired = False

    def _ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000) & 0xFFFFFFFF

    # ------------------------------------------------------------ control law
    def _control(self, idx: int, a: SimAxis, dt: float):
        if not a.configured:
            return
        if self.estop or not a.enabled:
            a.v = 0.0
            return
        acc = a.max_accel
        vt = 0.0
        if a.mode == MODE_VELOCITY:
            if self.watchdog_expired:
                a.v_cmd, a.stopping = 0.0, True
            vt = a.v_cmd
            acc = a.max_accel if a.stopping else a.vel_accel
            a.guard_dir = a.endstop_dir
        elif a.mode == MODE_POSITION:
            acc = a.move_accel
            err = a.target - a.pos
            if abs(err) < 1.0 and abs(a.v) <= acc * dt * 4:
                a.pos, a.v, a.mode = float(a.target), 0.0, MODE_IDLE
                self._emit("EVENT", type=P.EV_MOVE_DONE, axis=idx, value=a.target)
                return
            vb = math.sqrt(2.0 * 0.9 * acc * abs(err))
            vt = math.copysign(min(a.move_vmax, vb), err)
            a.guard_dir = a.endstop_dir
        elif a.mode == MODE_HOMING:
            vt = a.home_vel
            a.guard_dir = 1 if a.home_vel > 0 else -1
            if abs(a.pos - a.home_start) > a.home_max_travel:
                a.mode, a.v = MODE_IDLE, 0.0
                self._emit("EVENT", type=P.EV_HOME_FAILED, axis=idx, value=int(a.pos))
                return
        vt = max(-a.max_vel, min(a.max_vel, vt))

        was_at_limit = a.at_limit
        a.at_limit = False
        if a.lim_en and a.mode != MODE_HOMING:
            up, dn = a.lim_max - a.pos, a.pos - a.lim_min
            vup = math.sqrt(2 * 0.9 * acc * up) if up > 0 else 0.0
            vdn = -math.sqrt(2 * 0.9 * acc * dn) if dn > 0 else 0.0
            if vt > vup:
                vt, a.at_limit = vup, True
            if vt < vdn:
                vt, a.at_limit = vdn, True
            if a.at_limit and not was_at_limit:
                self._emit("EVENT", type=P.EV_LIMIT_HIT, axis=idx, value=int(a.pos))

        if a.halted:
            if vt * a.guard_dir <= 0:
                a.halted = False
            else:
                vt, a.v = 0.0, 0.0

        dv = acc * dt
        a.v += max(-dv, min(dv, vt - a.v))
        if a.at_limit and ((a.v > 0 and a.pos >= a.lim_max) or (a.v < 0 and a.pos <= a.lim_min)):
            a.v = 0.0

        # "ISR": integrate and check endstop guard
        if a.v != 0.0:
            moving_dir = 1 if a.v > 0 else -1
            if a.guard_dir != 0 and moving_dir == a.guard_dir and a.endstop_active():
                self._trigger(idx, a)
                return
            a.pos += a.v * dt

    def _trigger(self, idx, a: SimAxis):
        a.v = 0.0
        a.halted = True
        ev = P.EV_HOME_TRIGGERED if a.mode == MODE_HOMING else P.EV_ENDSTOP_HIT
        a.mode = MODE_IDLE
        self._emit("EVENT", type=ev, axis=idx, value=int(round(a.pos)))

    async def _run(self):
        last = time.monotonic()
        next_status = last
        while True:
            await asyncio.sleep(0.002)
            now = time.monotonic()
            if (self.watchdog_ms and not self.watchdog_expired
                    and (now - self.last_feed) * 1000 > self.watchdog_ms
                    and any(a.mode == MODE_VELOCITY and a.v_cmd != 0 for a in self.axes)):
                self.watchdog_expired = True
                self._emit("EVENT", type=P.EV_WATCHDOG, axis=0, value=0)
            steps = max(1, min(100, int(round((now - last) / CONTROL_DT))))
            for _ in range(steps):
                for i, a in enumerate(self.axes):
                    self._control(i, a, CONTROL_DT)
            last = now
            if now >= next_status:
                next_status = now + 1.0 / self.status_rate
                self._send_status()

    def _send_status(self):
        sys_flags = ((P.SYS_ESTOP if self.estop else 0)
                     | (P.SYS_WATCHDOG if self.watchdog_expired else 0)
                     | (P.SYS_TMC_READY if self.tmc_ready else 0))
        fields = {"time_ms": self._ms(), "sys_flags": sys_flags}
        for i, a in enumerate(self.axes):
            flags = 0
            if a.configured:
                flags |= P.ST_CONFIGURED
                flags |= P.ST_ENABLED if a.enabled else 0
                flags |= P.ST_MOVING if (a.v != 0 or a.mode in (MODE_POSITION, MODE_HOMING)) else 0
                flags |= P.ST_ENDSTOP if a.endstop_active() else 0
                flags |= P.ST_HOMING if a.mode == MODE_HOMING else 0
                flags |= P.ST_AT_LIMIT if a.at_limit else 0
                flags |= P.ST_HALTED if a.halted else 0
                flags |= P.ST_POSITION_MODE if a.mode == MODE_POSITION else 0
            fields[f"pos{i}"] = int(round(a.pos))
            fields[f"vel{i}"] = float(a.v)
            fields[f"flags{i}"] = flags
        self._emit("STATUS", 0, **fields)
