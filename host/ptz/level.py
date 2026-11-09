"""Base levelling: pan / tilt in a level frame when the mount stands at an angle.

If the base is not level, a pure pan sweep in the mount's own frame rises on
one side and dips on the other. With levelling on, jogging works in a level
("world") frame: a pan jog keeps the camera at the same elevation above the
horizon, and the tilt follows automatically.

The base attitude is given in the mount's own tilt units and sign, so that
it does not depend on which way the axes turn:

    pitch = tilt reading that is level when the head is at pan 0
    roll  = tilt reading that is level when the head is at pan +90

(e.g. pitch 3 and roll 0: the base leans so that at pan 0 the camera must
sit at tilt +3 to look level). Both can also be learned: aim the camera
level (camera's electronic level, or the horizon) at two pan angles at least
~30 degrees apart, and mark each one.

Assumes tilt 0 is perpendicular to the pan axis. A two-axis head cannot
remove the roll of the image itself: only the aim is corrected.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

Vec = Tuple[float, float, float]


def _norm(v: Vec) -> Vec:
    n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    return (v[0] / n, v[1] / n, v[2] / n)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


class Level:
    """Mount (pan, tilt) <-> level (pan, tilt), degrees.

    Mount frame: d = (cos t cos p, cos t sin p, sin t). The level "up" u is
    the vector that makes d.u = 0 at (0, pitch) and (90, roll). Level pan 0 is
    the mount's pan 0 direction projected on the level plane."""

    def __init__(self, pitch: float = 0.0, roll: float = 0.0):
        self.pitch, self.roll = float(pitch), float(roll)
        a, b = math.tan(math.radians(self.pitch)), math.tan(math.radians(self.roll))
        self.u = _norm((-a, -b, 1.0))
        ux = self.u[0]
        self.xw = _norm((1.0 - ux * self.u[0], -ux * self.u[1], -ux * self.u[2]))
        self.yw = _cross(self.u, self.xw)

    @property
    def identity(self) -> bool:
        return abs(self.pitch) < 1e-9 and abs(self.roll) < 1e-9

    def to_world(self, p: float, t: float) -> Tuple[float, float]:
        pr, tr = math.radians(p), math.radians(t)
        d = (math.cos(tr) * math.cos(pr), math.cos(tr) * math.sin(pr), math.sin(tr))
        e = math.degrees(math.asin(max(-1.0, min(1.0, _dot(d, self.u)))))
        az = math.degrees(math.atan2(_dot(d, self.yw), _dot(d, self.xw)))
        return p + wrap180(az - p), e          # level pan kept near the mount pan

    def to_mount(self, P: float, T: float) -> Tuple[float, float]:
        pr, tr = math.radians(P), math.radians(T)
        c = math.cos(tr)
        d = tuple(c * math.cos(pr) * self.xw[i] + c * math.sin(pr) * self.yw[i]
                  + math.sin(tr) * self.u[i] for i in range(3))
        t = math.degrees(math.asin(max(-1.0, min(1.0, d[2]))))
        p = math.degrees(math.atan2(d[1], d[0]))
        return P + wrap180(p - P), t

    def mount_rates(self, p: float, t: float, wp: float, wt: float,
                    hold: Optional[float] = None, gain: float = 2.0,
                    max_corr: float = 2.0) -> Tuple[float, float]:
        """Mount (pan, tilt) velocities giving level rates (wp, wt) at the
        mount position (p, t). hold: level tilt to keep while wt == 0 (slow
        correction of any drift, at most max_corr units/s)."""
        P, T = self.to_world(p, t)
        if hold is not None:
            wt += max(-max_corr, min(max_corr, gain * (hold - T)))
        h = 1e-3                                    # s, finite difference step
        p1, t1 = self.to_mount(P + wp * h, T + wt * h)
        return wrap180(p1 - p) / h, (t1 - t) / h


def solve(samples: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """(pitch, roll) from level marks [(mount pan, mount tilt), ...].

    tan(t_i) = A cos p_i + B sin p_i (least squares). With one mark, or
    marks too close together, the base is assumed to lean along the pan of
    the marks (smallest correction that fits them)."""
    if not samples:
        return 0.0, 0.0
    rows = [(math.cos(math.radians(p)), math.sin(math.radians(p)), math.tan(math.radians(t)))
            for p, t in samples]
    s11 = sum(c * c for c, _, _ in rows)
    s12 = sum(c * s for c, s, _ in rows)
    s22 = sum(s * s for _, s, _ in rows)
    r1 = sum(c * y for c, _, y in rows)
    r2 = sum(s * y for _, s, y in rows)
    det = s11 * s22 - s12 * s12
    if det > 0.05 * len(rows) ** 2:              # spread enough (> ~15-20 degrees)
        A, B = (r1 * s22 - r2 * s12) / det, (s11 * r2 - s12 * r1) / det
    else:                                         # minimum norm along the mean direction
        c = sum(c for c, _, _ in rows) / len(rows)
        s = sum(s for _, s, _ in rows) / len(rows)
        y = sum(y for _, _, y in rows) / len(rows)
        n = c * c + s * s
        A, B = (y * c / n, y * s / n) if n > 1e-9 else (0.0, 0.0)
    return math.degrees(math.atan(A)), math.degrees(math.atan(B))


def spread(samples: Iterable[Tuple[float, float]]) -> float:
    """Largest pan difference between marks (degrees)."""
    ps = [p for p, _ in samples]
    return max(ps) - min(ps) if ps else 0.0
