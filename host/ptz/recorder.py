"""Movement recorder.

Two modes:
  continuous  the actual path is sampled (RATE_HZ) while recording, whatever
              moves the head (web pad, gamepad, UDP joystick, VISCA, presets)
  keypoints   only the positions captured with "keypoint" are stored, with
              the time at which they were captured; replay glides through
              them smoothly (monotone spline, ease in at start / out at end)

Option "start when moving": the recorder is armed and the clock only starts
when the head starts to move, so the time spent clicking is not recorded;
the motionless end of the recording is trimmed as well.

Recordings are JSON files in <state_dir>/recordings/. They can be
downloaded and uploaded (same format, validated on upload).
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from .track import TrackError, average_laps, split_laps

RATE_HZ = 20.0
MODES = ("continuous", "keypoints", "laps")
# Recording kinds on disk: continuous, keypoints, laps-raw (learning session
# with lap marks), track (averaged lap built from a laps-raw session)
EXTRA_KEYS = ("markers", "laps", "lap_time", "source")
MOVE_EPS = 0.01       # units: movement that starts an armed recording
TRIM_EPS = 0.01       # units: tolerance of the motionless end
MAX_POINTS = 200_000


class RecorderError(Exception):
    pass


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9 _.()-]", "_", name).strip(" .")
    return name[:60] or time.strftime("rec-%Y%m%d-%H%M%S")


def trim_idle_end(points: List[dict]) -> List[dict]:
    """Drop the motionless tail, keeping the point where the motion ended."""
    end = points[-1]["pos"]
    k = len(points) - 1
    while k > 0 and all(abs(points[k - 1]["pos"].get(n, v) - v) <= TRIM_EPS
                        for n, v in end.items()):
        k -= 1
    return points[:max(k, 1) + 1]


class Recorder:
    def __init__(self, directory: str, positions: Callable[[], Dict[str, float]],
                 clock: Callable[[], float]):
        self.dir = directory
        self._positions = positions          # homed axes only, user units
        self._clock = clock
        self.active = False
        self.mode = "continuous"
        self.name = ""
        self.points: List[dict] = []
        self.markers: List[float] = []       # lap marks (laps mode)
        self.armed = False                   # waiting for the first movement
        self.trim = False
        self._baseline: Dict[str, float] = {}
        self._t0 = 0.0
        self._last_sample = 0.0

    # ------------------------------------------------------------ recording
    def start(self, mode: str = "continuous", name: str = "",
              on_move: bool = False) -> None:
        if self.active:
            raise RecorderError("already recording")
        if mode not in MODES:
            raise RecorderError(f"mode must be one of {MODES}")
        if not self._positions():
            raise RecorderError("home the axes before recording")
        self.mode, self.name = mode, _safe_name(name) if name else ""
        self.points, self.markers, self.active = [], [], True
        if mode == "laps":
            on_move = False                  # the lap marks define the useful part
        self.armed = self.trim = bool(on_move)
        if self.armed:
            self._baseline = self._positions()
        else:
            self._t0 = self._clock()
            self._add()                      # starting point

    def on_status(self) -> None:
        """Called on every MCU status: movement detection and sampling."""
        if not self.active:
            return
        now = self._clock()
        if self.armed:
            pos = self._positions()
            if any(abs(pos.get(n, v) - v) > MOVE_EPS for n, v in self._baseline.items()):
                # movement started between the previous status and this one
                self.armed = False
                self._t0 = now - 1.0 / RATE_HZ
                self.points = [{"t": 0.0, "pos": {n: round(v, 4)
                                                  for n, v in self._baseline.items()}}]
                self._add(now)
            return
        if self.mode in ("continuous", "laps") and now - self._last_sample >= 1.0 / RATE_HZ:
            self._add(now)

    def lap_mark(self) -> int:
        """Learning mode: the car passes the start / timing line now."""
        if not self.active or self.mode != "laps":
            raise RecorderError("lap marks are only used while learning a track")
        now = self._clock()
        self._add(now)                       # exact position at the mark
        self.markers.append(round(now - self._t0, 4))
        return len(self.markers)

    def keypoint(self) -> int:
        if not self.active:
            raise RecorderError("not recording")
        if self.armed:
            raise RecorderError("waiting for the first movement")
        if self.mode != "keypoints":
            raise RecorderError("keypoints are only used in keypoints mode")
        self._add()
        return len(self.points)

    def stop(self) -> dict:
        if not self.active:
            raise RecorderError("not recording")
        self.active = False
        if self.armed:
            self.armed = False
            raise RecorderError("nothing recorded: the head never moved")
        last = self.points[-1]["pos"]
        now_pos = self._positions()
        moved = any(abs(now_pos.get(n, v) - v) > 1e-3 for n, v in last.items())
        if self.mode in ("continuous", "laps") or moved:
            self._add()
        if self.mode == "laps":
            return self._finish_learning()
        if self.trim:
            self.points = trim_idle_end(self.points)
        if len(self.points) < 2:
            raise RecorderError("nothing recorded (need at least 2 points)")
        name = self.name or time.strftime("rec-%Y%m%d-%H%M%S")
        return self._save({"name": name, "mode": self.mode, "points": self.points})

    def _finish_learning(self) -> dict:
        name = self.name or time.strftime("track-%Y%m%d-%H%M")
        if len(self.markers) < 2:
            raise RecorderError("no complete lap: press the lap key each time you pass "
                                "the start line (at least twice)")
        raw = self._save({"name": f"{name} (raw)", "mode": "laps-raw",
                          "points": self.points, "markers": self.markers})
        return self.build_track(raw["name"], name=name)

    def build_track(self, raw_name: str, exclude: Tuple[int, ...] = (),
                    name: str = "") -> dict:
        """(Re)build the averaged lap of a learning session."""
        raw = self.load(raw_name)
        if raw.get("mode") != "laps-raw":
            raise RecorderError(f"'{raw_name}' is not a learning session")
        try:
            laps = split_laps(raw["points"], raw.get("markers", []))
            points, stats = average_laps(laps, tuple(exclude), auto_reject=not exclude)
        except TrackError as e:
            raise RecorderError(str(e)) from None
        if not name:
            name = raw["name"][:-6] if raw["name"].endswith(" (raw)") else raw["name"] + " track"
        used = [s for s in stats if s["used"]]
        return self._save({"name": name, "mode": "track", "points": points,
                           "laps": stats, "source": raw["name"],
                           "lap_time": round(sum(s["duration"] for s in used) / len(used), 3)})

    def cancel(self) -> None:
        self.active = self.armed = False
        self.points = []
        self.markers = []

    def _save(self, data: dict, overwrite: bool = True) -> dict:
        name = _safe_name(data["name"])
        if not overwrite:
            base, i = name, 2
            while os.path.exists(self._path(name)):
                name, i = f"{base} ({i})", i + 1
        extra = {k: data[k] for k in EXTRA_KEYS if k in data}
        data = {"name": name, "mode": data.get("mode", "continuous"),
                "created": time.time(), "duration": round(data["points"][-1]["t"], 3),
                **extra, "points": data["points"]}
        os.makedirs(self.dir, exist_ok=True)
        path = self._path(name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return self._summary(data)

    def import_data(self, data, limits: Dict[str, tuple], name: str = "") -> dict:
        """Validate and store an uploaded recording. `limits` maps every
        configured axis to (min, max). Never overwrites an existing one."""
        if not isinstance(data, dict) or not isinstance(data.get("points"), list):
            raise RecorderError("not a recording: expected an object with a 'points' list")
        pts = data["points"]
        if not 2 <= len(pts) <= MAX_POINTS:
            raise RecorderError(f"a recording needs 2 to {MAX_POINTS} points")
        axes = None
        clean, prev_t = [], None
        for i, p in enumerate(pts):
            try:
                t = float(p["t"])
                pos = {str(k): float(v) for k, v in p["pos"].items()}
            except (KeyError, TypeError, ValueError, AttributeError):
                raise RecorderError(
                    f"point {i}: expected an object with 't' (seconds) and 'pos' "
                    "(axis: position)") from None
            if axes is None:
                axes = set(pos)
                unknown = axes - set(limits)
                if unknown:
                    raise RecorderError(f"unknown axis {', '.join(sorted(unknown))}")
                if not axes:
                    raise RecorderError("points have no axis")
            elif set(pos) != axes:
                raise RecorderError(f"point {i}: every point must have the same axes")
            if prev_t is not None and t <= prev_t:
                raise RecorderError(f"point {i}: times must be strictly increasing")
            for n, v in pos.items():
                lo, hi = limits[n]
                if not lo <= v <= hi:
                    raise RecorderError(f"point {i}: {n} = {v} is outside {lo}..{hi}")
            clean.append({"t": round(t - float(pts[0]["t"]), 4), "pos": pos})
            prev_t = t
        mode = data.get("mode", "continuous")
        if mode not in ("continuous", "keypoints", "track", "laps-raw"):
            mode = "continuous"
        out = {"name": name or data.get("name") or "uploaded", "mode": mode, "points": clean}
        if mode == "track" and isinstance(data.get("lap_time"), (int, float)):
            out["lap_time"] = float(data["lap_time"])
        if mode == "laps-raw" and isinstance(data.get("markers"), list):
            t0 = float(pts[0]["t"])
            out["markers"] = [float(m) - t0 for m in data["markers"]
                              if isinstance(m, (int, float))]
        return self._save(out, overwrite=False)

    def _add(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        self._last_sample = now
        t = round(now - self._t0, 4)
        if self.points and t <= self.points[-1]["t"]:
            t = self.points[-1]["t"] + 0.01     # keep times strictly increasing
        self.points.append({"t": t, "pos": {n: round(v, 4) for n, v in self._positions().items()}})

    def state(self) -> dict:
        if not self.active:
            return {"active": False}
        if self.armed:
            return {"active": True, "armed": True, "mode": self.mode, "points": 0,
                    "elapsed": 0.0}
        elapsed = self._clock() - self._t0
        st = {"active": True, "armed": False, "mode": self.mode,
              "points": len(self.points), "elapsed": round(elapsed, 1)}
        if self.mode == "laps":
            st["marks"] = len(self.markers)
            st["laps"] = max(0, len(self.markers) - 1)
            st["lap_elapsed"] = round(elapsed - self.markers[-1], 1) if self.markers else None
            if len(self.markers) >= 2:
                st["last_lap"] = round(self.markers[-1] - self.markers[-2], 2)
        return st

    # ------------------------------------------------------------ library
    def _path(self, name: str) -> str:
        return os.path.join(self.dir, _safe_name(name) + ".json")

    def load(self, name: str) -> dict:
        try:
            with open(self._path(name), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            raise RecorderError(f"no recording named '{name}'") from None

    def delete(self, name: str) -> None:
        try:
            os.remove(self._path(name))
        except FileNotFoundError:
            raise RecorderError(f"no recording named '{name}'") from None

    def rename(self, name: str, new: str) -> None:
        data = self.load(name)
        data["name"] = _safe_name(new)
        dst = self._path(new)
        if os.path.exists(dst):
            raise RecorderError(f"'{new}' already exists")
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.remove(self._path(name))

    def list(self) -> List[dict]:
        out = []
        if os.path.isdir(self.dir):
            for fn in os.listdir(self.dir):
                if fn.endswith(".json"):
                    try:
                        with open(os.path.join(self.dir, fn), encoding="utf-8") as f:
                            out.append(self._summary(json.load(f)))
                    except (OSError, ValueError):
                        continue
        return sorted(out, key=lambda r: r.get("created", 0), reverse=True)

    @staticmethod
    def _summary(d: dict) -> dict:
        extra = {}
        if d.get("mode") == "track":
            extra = {"lap_time": d.get("lap_time"), "laps": d.get("laps", []),
                     "source": d.get("source")}
        elif d.get("mode") == "laps-raw":
            extra = {"marks": len(d.get("markers", []))}
        return {"name": d["name"], "mode": d.get("mode", "continuous"), **extra,
                "duration": d.get("duration", 0), "points": len(d.get("points", [])),
                "axes": sorted(d["points"][0]["pos"]) if d.get("points") else [],
                "created": d.get("created", 0)}
