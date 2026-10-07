"""Host-side motion shaping (pure Python, no hardware dependency).

The Raspberry Pi computes smooth motion and streams velocities to the RP2040
at ~100 Hz. The MCU only follows (with its own hard acceleration cap, soft
limits and watchdog). Keeping the shaping here makes it easy to tune.

  SCurveProfile      normalized 0 -> 1 move with acceleration limit and
                     "ease in/out" (jerk limit), used by goto / presets
  VelocityShaper     jerk-limited velocity follower, used by jog
  Pchip              monotone cubic interpolation, used by recording replay
  MoveTrajectory     synchronized multi-axis point-to-point move
  PlaybackTrajectory replay of a recording at a variable speed factor
"""
from __future__ import annotations

import bisect
import math
from typing import Callable, Dict, List, Sequence, Tuple


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


# ============================================================ S-curve profile
class SCurveProfile:
    """Normalized move s: 0 -> 1.

    A trapezoidal velocity profile (limits vmax, amax) filtered by a moving
    average of `smooth` seconds. The filter turns the acceleration steps into
    linear ramps: jerk = amax / smooth, i.e. an ease-in / ease-out, while the
    distance stays exactly 1. Total duration = trapezoid duration + smooth.
    """

    def __init__(self, vmax: float, amax: float, smooth: float = 0.0):
        vmax, amax = max(vmax, 1e-9), max(amax, 1e-9)
        if vmax * vmax / amax >= 1.0:               # never reaches vmax
            ta = math.sqrt(1.0 / amax)
            vpk, tc = amax * ta, 0.0
        else:
            ta, vpk = vmax / amax, vmax
            tc = (1.0 - vpk * ta) / vpk
        self.a, self.ta, self.tc, self.vpk = amax, ta, tc, vpk
        self.T = 2.0 * ta + tc
        self.tj = max(0.0, smooth)
        self.duration = self.T + self.tj
        self._p1 = 0.5 * amax * ta * ta
        self._P1 = amax * ta ** 3 / 6.0
        self._p2 = self._p1 + vpk * tc
        self._P2 = self._P1 + self._p1 * tc + 0.5 * vpk * tc * tc
        self._PT = self._P_trap(self.T)

    # trapezoid position / velocity
    def _p(self, t: float) -> float:
        if t <= 0.0:
            return 0.0
        if t <= self.ta:
            return 0.5 * self.a * t * t
        if t <= self.ta + self.tc:
            return self._p1 + self.vpk * (t - self.ta)
        if t < self.T:
            u = t - self.ta - self.tc
            return self._p2 + self.vpk * u - 0.5 * self.a * u * u
        return 1.0

    def _v(self, t: float) -> float:
        if t <= 0.0 or t >= self.T:
            return 0.0
        if t <= self.ta:
            return self.a * t
        if t <= self.ta + self.tc:
            return self.vpk
        return self.vpk - self.a * (t - self.ta - self.tc)

    # integral of the trapezoid position, for the moving average
    def _P_trap(self, t: float) -> float:
        if t <= self.ta:
            return self.a * t ** 3 / 6.0
        if t <= self.ta + self.tc:
            u = t - self.ta
            return self._P1 + self._p1 * u + 0.5 * self.vpk * u * u
        u = t - self.ta - self.tc
        return self._P2 + self._p2 * u + 0.5 * self.vpk * u * u - self.a * u ** 3 / 6.0

    def _P(self, t: float) -> float:
        if t <= 0.0:
            return 0.0
        if t >= self.T:
            return self._PT + (t - self.T)
        return self._P_trap(t)

    def sample(self, t: float) -> Tuple[float, float]:
        """(s, ds/dt) at time t."""
        if t >= self.duration:
            return 1.0, 0.0
        if self.tj <= 1e-6:
            return self._p(t), self._v(t)
        return ((self._P(t) - self._P(t - self.tj)) / self.tj,
                (self._p(t) - self._p(t - self.tj)) / self.tj)


# ============================================================ jog shaper
class VelocityShaper:
    """Follows a target velocity with an acceleration limit and an optional
    jerk limit (ease in/out). Units are whatever the caller uses."""

    def __init__(self):
        self.v = 0.0
        self.a = 0.0

    def reset(self, v: float = 0.0) -> None:
        self.v, self.a = v, 0.0

    def step(self, target: float, amax: float, jerk: float, dt: float) -> float:
        if dt <= 0:
            return self.v
        if jerk <= 0 or math.isinf(jerk):
            lim = amax * dt
            self.v += clamp(target - self.v, -lim, lim)
            self.a = 0.0
            return self.v
        dv = target - self.v
        # velocity still gained if the acceleration is brought to 0 right now
        coast = self.a * abs(self.a) / (2.0 * jerk)
        err = dv - coast
        if abs(err) <= jerk * dt * dt:
            a_target = 0.0
        else:
            a_target = math.copysign(amax, err)
        self.a += clamp(a_target - self.a, -jerk * dt, jerk * dt)
        v_new = self.v + self.a * dt
        # crossing the target with little acceleration left: land exactly
        if (target - self.v) * (target - v_new) <= 0 and abs(self.a) <= 2.0 * jerk * dt:
            v_new, self.a = target, 0.0
        # inside the dead zone with no acceleration left the error would never
        # shrink (residual creep with uneven tick times): land exactly
        elif abs(target - v_new) <= jerk * dt * dt and abs(self.a) <= jerk * dt:
            v_new, self.a = target, 0.0
        self.v = v_new
        return self.v


def braking_speed(dist: float, amax: float, smooth: float) -> float:
    """Highest speed from which the shaper can stop within `dist`."""
    if dist <= 0:
        return 0.0
    # distance needed from speed v: v^2 / (2a) + v * smooth / 2
    h = smooth / 2.0
    return amax * (-h + math.sqrt(h * h + 2.0 * dist / amax))


# ============================================================ PCHIP
class Pchip:
    """Monotone piecewise cubic (Fritsch-Carlson): goes through every point,
    never overshoots between them. End slopes are 0 (ease in / ease out),
    or follow the data with natural_ends=True (race tracking: the car crosses
    the start line at full speed)."""

    def __init__(self, ts: Sequence[float], ys: Sequence[float], natural_ends: bool = False):
        if len(ts) != len(ys) or not ts:
            raise ValueError("Pchip needs matching, non-empty sequences")
        self.ts, self.ys = list(ts), list(ys)
        n = len(ts)
        self.m = [0.0] * n
        if n < 3:
            return
        h = [ts[k + 1] - ts[k] for k in range(n - 1)]
        d = [(ys[k + 1] - ys[k]) / h[k] if h[k] > 0 else 0.0 for k in range(n - 1)]
        for k in range(1, n - 1):
            if d[k - 1] * d[k] <= 0:
                continue
            w1, w2 = 2 * h[k] + h[k - 1], h[k] + 2 * h[k - 1]
            self.m[k] = (w1 + w2) / (w1 / d[k - 1] + w2 / d[k])
        if natural_ends:
            self.m[0] = self._end_slope(h[0], h[1], d[0], d[1])
            self.m[-1] = self._end_slope(h[-1], h[-2], d[-1], d[-2])

    @staticmethod
    def _end_slope(h0: float, h1: float, d0: float, d1: float) -> float:
        """Shape-preserving 3-point end slope (as scipy's PCHIP)."""
        if h0 + h1 <= 0:
            return d0
        m = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        if m * d0 <= 0:
            return 0.0
        if d0 * d1 <= 0 and abs(m) > abs(3 * d0):
            return 3 * d0
        return m

    @property
    def duration(self) -> float:
        return self.ts[-1] - self.ts[0]

    def __call__(self, t: float) -> Tuple[float, float]:
        ts, ys, m = self.ts, self.ys, self.m
        if len(ts) == 1 or t <= ts[0]:
            return ys[0], 0.0
        if t >= ts[-1]:
            return ys[-1], 0.0
        k = bisect.bisect_right(ts, t) - 1
        h = ts[k + 1] - ts[k]
        if h <= 0:
            return ys[k + 1], 0.0
        s = (t - ts[k]) / h
        s2, s3 = s * s, s * s * s
        y = ((2 * s3 - 3 * s2 + 1) * ys[k] + (s3 - 2 * s2 + s) * h * m[k]
             + (-2 * s3 + 3 * s2) * ys[k + 1] + (s3 - s2) * h * m[k + 1])
        dy = ((6 * s2 - 6 * s) * ys[k] + (3 * s2 - 4 * s + 1) * h * m[k]
              + (-6 * s2 + 6 * s) * ys[k + 1] + (3 * s2 - 2 * s) * h * m[k + 1]) / h
        return y, dy


# ============================================================ trajectories
class MoveTrajectory:
    """Straight-line (in joint space) synchronized move with ease in/out."""

    def __init__(self, start: Dict[str, float], target: Dict[str, float],
                 vmax: Dict[str, float], amax: Dict[str, float], smooth: float):
        self.start, self.final = dict(start), dict(target)
        self.delta = {n: target[n] - start[n] for n in target}
        moving = [n for n, d in self.delta.items() if abs(d) > 1e-9]
        if moving:
            vs = min(vmax[n] / abs(self.delta[n]) for n in moving)
            as_ = min(amax[n] / abs(self.delta[n]) for n in moving)
            self.profile = SCurveProfile(vs, as_, smooth)
            self.duration = self.profile.duration
        else:
            self.profile, self.duration = None, 0.0
        self.axes: List[str] = list(target)
        self.t = 0.0

    @property
    def done(self) -> bool:
        return self.t >= self.duration

    @property
    def progress(self) -> float:
        return 1.0 if self.duration <= 0 else min(1.0, self.t / self.duration)

    def advance(self, dt: float) -> None:
        self.t += dt

    def sample(self) -> Dict[str, Tuple[float, float]]:
        if self.profile is None:
            return {n: (self.final[n], 0.0) for n in self.axes}
        s, ds = self.profile.sample(self.t)
        return {n: (self.start[n] + self.delta[n] * s, self.delta[n] * ds) for n in self.axes}


class PlaybackTrajectory:
    """Replays recorded points (time, positions) through monotone splines.

    `speed()` is read every tick, so the replay speed can change while
    playing. The local speed is reduced automatically where the recording
    would exceed an axis max velocity at the requested factor.
    """

    SPEED_SLEW = 2.0          # max change of the speed factor per second

    def __init__(self, points: List[dict], vmax: Dict[str, float],
                 speed: Callable[[], float], natural_ends: bool = False):
        ts = [p["t"] for p in points]
        names = [n for n in points[0]["pos"] if n in vmax]
        self.splines = {n: Pchip(ts, [p["pos"][n] for p in points], natural_ends)
                        for n in names}
        self.t0, self.duration = ts[0], ts[-1] - ts[0]
        self.vmax = vmax
        self.speed = speed
        self.axes = names
        self.final = {n: points[-1]["pos"][n] for n in names}
        self.tau = 0.0                      # position in the recording (s)
        self._k = max(0.05, speed())         # current, slewed speed factor
        self._last: Dict[str, Tuple[float, float]] = {}

    @property
    def done(self) -> bool:
        return self.tau >= self.duration

    @property
    def progress(self) -> float:
        return 1.0 if self.duration <= 0 else min(1.0, self.tau / self.duration)

    def advance(self, dt: float) -> None:
        want = max(0.05, self.speed())
        step = self.SPEED_SLEW * dt
        self._k += clamp(want - self._k, -step, step)
        k = self._k
        # local slow-down if the recording is too fast for the axes at this factor
        for n, sp in self.splines.items():
            _, dy = sp(self.t0 + self.tau)
            if abs(dy) * k > self.vmax[n] > 0:
                k = self.vmax[n] / abs(dy)
        self.tau = min(self.duration, self.tau + dt * k)
        here = {n: sp(self.t0 + self.tau) for n, sp in self.splines.items()}
        for n, (_, dy) in here.items():         # re-check the cap at the new point
            if abs(dy) * k > self.vmax[n] > 0:
                k = self.vmax[n] / abs(dy)
        self._last = {n: (y, dy * k if not self.done else 0.0) for n, (y, dy) in here.items()}

    def sample(self) -> Dict[str, Tuple[float, float]]:
        if not self._last:                       # not advanced yet: start state
            return self.initial()
        return dict(self._last)

    def initial(self) -> Dict[str, Tuple[float, float]]:
        """(position, velocity) at the start, at the initial speed factor."""
        # the spline's own start slope (calling it at the first point would
        # return the "outside the range" slope of 0)
        return {n: (sp.ys[0], sp.m[0] * self._k) for n, sp in self.splines.items()}


class BlendIn:
    """Joins a trajectory from another position and velocity.

    The difference between the current state and the start of `inner` is
    faded out with a cubic Hermite curve (same velocity at the start, zero
    offset and zero offset-velocity at the end), while `inner` already runs:
    the head glides onto the new path instead of jumping or stopping.
    """

    MIN_T, MAX_T = 0.3, 6.0

    def __init__(self, inner, start_pos: Dict[str, float], start_vel: Dict[str, float],
                 vmax: Dict[str, float], amax: Dict[str, float]):
        self.inner = inner
        self.axes = inner.axes
        self.final = inner.final
        s0 = inner.initial()
        self.o0 = {n: start_pos.get(n, s0[n][0]) - s0[n][0] for n in self.axes}
        self.v0 = {n: start_vel.get(n, s0[n][1]) - s0[n][1] for n in self.axes}
        T = self.MIN_T
        for n in self.axes:
            # The fade must use at most half of the axis limits (the lap uses
            # the rest): its speed peaks at 1.5*|o|/T, its acceleration at
            # 6|o|/T^2 + 4|v|/T (Hermite curve, at the start).
            o, v = abs(self.o0[n]), abs(self.v0[n])
            vm, am = max(vmax[n], 1e-9), max(amax[n], 1e-9)
            T = max(T, 3.0 * o / vm,
                    (4.0 * v + math.sqrt(16.0 * v * v + 12.0 * am * o)) / am)
        self.T = min(T, self.MAX_T)
        self.t = 0.0

    @property
    def duration(self) -> float:
        return self.inner.duration

    @property
    def done(self) -> bool:
        return self.inner.done

    @property
    def progress(self) -> float:
        return self.inner.progress

    def advance(self, dt: float) -> None:
        self.inner.advance(dt)
        self.t += dt

    def sample(self) -> Dict[str, Tuple[float, float]]:
        s = self.inner.sample()
        if self.t >= self.T:
            return s
        u, T = self.t / self.T, self.T
        h00, h10 = 2 * u ** 3 - 3 * u ** 2 + 1, u ** 3 - 2 * u ** 2 + u
        d00, d10 = 6 * u ** 2 - 6 * u, 3 * u ** 2 - 4 * u + 1
        out = {}
        for n, (p, v) in s.items():
            o, w = self.o0[n], self.v0[n] * T
            out[n] = (p + h00 * o + h10 * w, v + (d00 * o + d10 * w) / T)
        return out
