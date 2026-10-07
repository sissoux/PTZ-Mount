"""Movement recorder.

Two modes:
  continuous  the actual path is sampled (RATE_HZ) while recording, whatever
              moves the head (web pad, gamepad, UDP joystick, VISCA, presets)
  keypoints   only the positions captured with "keypoint" are stored, with
              the time at which they were captured; replay glides through
              them smoothly (monotone spline, ease in at start / out at end)

Recordings are JSON files in <state_dir>/recordings/.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Dict, List, Optional

RATE_HZ = 20.0
MODES = ("continuous", "keypoints")


class RecorderError(Exception):
    pass


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9 _.-]", "_", name).strip(" .")
    return name[:60] or time.strftime("rec-%Y%m%d-%H%M%S")


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
        self._t0 = 0.0
        self._last_sample = 0.0

    # ------------------------------------------------------------ recording
    def start(self, mode: str = "continuous", name: str = "") -> None:
        if self.active:
            raise RecorderError("already recording")
        if mode not in MODES:
            raise RecorderError(f"mode must be one of {MODES}")
        if not self._positions():
            raise RecorderError("home the axes before recording")
        self.mode, self.name = mode, _safe_name(name) if name else ""
        self.points, self.active = [], True
        self._t0 = self._clock()
        self._add()                          # starting point

    def on_status(self) -> None:
        """Called on every MCU status (continuous mode sampling)."""
        if self.active and self.mode == "continuous":
            now = self._clock()
            if now - self._last_sample >= 1.0 / RATE_HZ:
                self._add(now)

    def keypoint(self) -> int:
        if not self.active:
            raise RecorderError("not recording")
        if self.mode != "keypoints":
            raise RecorderError("keypoints are only used in keypoints mode")
        self._add()
        return len(self.points)

    def stop(self) -> dict:
        if not self.active:
            raise RecorderError("not recording")
        self.active = False
        last = self.points[-1]["pos"]
        now_pos = self._positions()
        moved = any(abs(now_pos.get(n, v) - v) > 1e-3 for n, v in last.items())
        if self.mode == "continuous" or moved:
            self._add()
        if len(self.points) < 2:
            raise RecorderError("nothing recorded (need at least 2 points)")
        name = self.name or time.strftime("rec-%Y%m%d-%H%M%S")
        data = {"name": name, "mode": self.mode, "created": time.time(),
                "duration": round(self.points[-1]["t"], 3), "points": self.points}
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, name + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return self._summary(data)

    def cancel(self) -> None:
        self.active = False
        self.points = []

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
        return {"active": True, "mode": self.mode, "points": len(self.points),
                "elapsed": round(self._clock() - self._t0, 1)}

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
        return {"name": d["name"], "mode": d.get("mode", "continuous"),
                "duration": d.get("duration", 0), "points": len(d.get("points", [])),
                "axes": sorted(d["points"][0]["pos"]) if d.get("points") else [],
                "created": d.get("created", 0)}
