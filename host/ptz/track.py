"""Race tracking: build one lap path from several laps followed by hand.

Learning session: the operator records while following cars for several
laps and marks every pass at the start / timing line (Space in the web UI).
The recording is cut into laps at those marks; each lap is resampled on a
normalized lap phase (0 = start line, 1 = start line again) and the laps are
averaged point by point. Laps far from the others (missed car, late mark...)
are rejected automatically. The averaged lap keeps the mean lap time, so
replaying it at 1x follows a "regular" car.
"""
from __future__ import annotations

import bisect
import math
import statistics
from typing import Dict, List, Tuple

MIN_LAP_S = 2.0            # marks closer than this are double presses: ignored
RESAMPLE_HZ = 20.0         # points per second in the averaged lap
REJECT_FACTOR = 3.0        # lap rejected if its deviation > factor * median
REJECT_FLOOR = 0.5         # ... and > this many units (never reject tiny noise)


class TrackError(Exception):
    pass


class _Series:
    """Linear interpolation over recorded points."""

    def __init__(self, points: List[dict]):
        self.ts = [p["t"] for p in points]
        self.pos = [p["pos"] for p in points]
        self.axes = list(points[0]["pos"])

    def at(self, t: float) -> Dict[str, float]:
        ts = self.ts
        if t <= ts[0]:
            return dict(self.pos[0])
        if t >= ts[-1]:
            return dict(self.pos[-1])
        k = bisect.bisect_right(ts, t) - 1
        t0, t1 = ts[k], ts[k + 1]
        w = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
        a, b = self.pos[k], self.pos[k + 1]
        return {n: a[n] + (b[n] - a[n]) * w for n in self.axes}


def split_laps(points: List[dict], markers: List[float]) -> List[List[dict]]:
    """Cut a recording into laps between consecutive start-line marks."""
    if len(points) < 2:
        raise TrackError("recording is empty")
    series = _Series(points)
    marks = []
    for m in sorted(markers):
        if not marks or m - marks[-1] >= MIN_LAP_S:
            marks.append(m)
    laps = []
    for a, b in zip(marks, marks[1:]):
        inner = [{"t": p["t"] - a, "pos": p["pos"]} for p in points if a < p["t"] < b]
        laps.append([{"t": 0.0, "pos": series.at(a)}] + inner
                    + [{"t": b - a, "pos": series.at(b)}])
    return laps


def _resample(lap: List[dict], n: int) -> List[Dict[str, float]]:
    s = _Series(lap)
    dur = lap[-1]["t"]
    return [s.at(dur * k / (n - 1)) for k in range(n)]


def _mean(samples: List[List[Dict[str, float]]], idx: List[int], axes) -> List[Dict[str, float]]:
    n = len(samples[0])
    return [{a: sum(samples[i][k][a] for i in idx) / len(idx) for a in axes} for k in range(n)]


def _rms(sample: List[Dict[str, float]], ref: List[Dict[str, float]], axes) -> float:
    tot = sum((sample[k][a] - ref[k][a]) ** 2 for k in range(len(ref)) for a in axes)
    return math.sqrt(tot / (len(ref) * len(axes)))


def average_laps(laps: List[List[dict]], exclude: Tuple[int, ...] = (),
                 auto_reject: bool = True) -> Tuple[List[dict], List[dict]]:
    """Average laps on a normalized phase.

    exclude: lap numbers (1-based) to leave out. Returns (points, stats);
    stats has one entry per lap: duration, deviation from the average (rms,
    in axis units) and whether it was used.
    """
    if not laps:
        raise TrackError("no complete lap: press the lap key at least twice "
                         "(start line, then start line again)")
    axes = list(laps[0][0]["pos"])
    durations = [lap[-1]["t"] for lap in laps]
    n = max(50, int(statistics.mean(durations) * RESAMPLE_HZ) + 1)
    samples = [_resample(lap, n) for lap in laps]
    used = [i for i in range(len(laps)) if (i + 1) not in exclude]
    if not used:
        raise TrackError("every lap is excluded")
    avg = _mean(samples, used, axes)
    if auto_reject and len(used) >= 3:
        dev = {i: _rms(samples[i], avg, axes) for i in used}
        limit = max(REJECT_FACTOR * statistics.median(dev.values()), REJECT_FLOOR)
        keep = [i for i in used if dev[i] <= limit]
        if 2 <= len(keep) < len(used):
            used = keep
            avg = _mean(samples, used, axes)
    lap_time = statistics.mean(durations[i] for i in used)
    points = [{"t": round(lap_time * k / (n - 1), 4),
               "pos": {a: round(v, 4) for a, v in avg[k].items()}} for k in range(n)]
    stats = [{"lap": i + 1, "duration": round(durations[i], 3),
              "deviation": round(_rms(samples[i], avg, axes), 3), "used": i in used}
             for i in range(len(laps))]
    return points, stats
