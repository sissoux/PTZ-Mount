"""Preset positions, persisted as JSON in the state directory."""
from __future__ import annotations

import json
import os
from typing import Dict, Optional


class PresetStore:
    def __init__(self, path: str):
        self.path = path
        self._data: Dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                self._data = json.load(f)
        except FileNotFoundError:
            self._data = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)

    def get(self, pid) -> Optional[dict]:
        p = self._data.get(str(pid))
        return dict(p["positions"]) if p else None

    def set(self, pid, positions: Dict[str, float], name: str = "") -> None:
        self._data[str(pid)] = {"name": name or f"Preset {pid}",
                                "positions": {k: round(v, 4) for k, v in positions.items()}}
        self._save()

    def delete(self, pid) -> None:
        if self._data.pop(str(pid), None) is not None:
            self._save()

    def all(self) -> Dict[str, dict]:
        return dict(self._data)
