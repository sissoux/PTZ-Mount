"""Editable configuration file (web settings page).

The repository file (config/ptz.cfg, passed with -c) is the default and is
never modified by the web UI, so `git pull` keeps working. Edits are saved to
<state_dir>/ptz.cfg, which is used instead of the default when it exists.
Every save keeps a timestamped backup of the previous content.
"""
from __future__ import annotations

import configparser
import os
import re
import tempfile
import time
from typing import List, Optional, Tuple

from .config import ConfigError, PtzConfig, load

MAX_BACKUPS = 30


class ConfigFiles:
    def __init__(self, default_path: str, state_dir: str):
        self.default = os.path.abspath(default_path)
        self.state_dir = state_dir
        self.override = os.path.join(state_dir, "ptz.cfg")
        self.backup_dir = os.path.join(state_dir, "config-backups")
        self.load_error = ""          # set when the edited file could not be used

    @property
    def has_override(self) -> bool:
        return os.path.exists(self.override)

    @property
    def active(self) -> str:
        return self.override if self.has_override else self.default

    def read(self) -> str:
        with open(self.active, encoding="utf-8") as f:
            return f.read()

    def load_active(self) -> PtzConfig:
        """Config to run with: the edited file, or the default if it is broken."""
        if self.has_override:
            try:
                cfg = load(self.override)
                self.load_error = ""
                return self._pin_state_dir(cfg)
            except (ConfigError, configparser.Error, ValueError, OSError) as e:
                self.load_error = (f"edited config {self.override} is invalid ({e}); "
                                   f"running with the default {self.default}")
        return self._pin_state_dir(load(self.default))

    def _pin_state_dir(self, cfg: PtzConfig) -> PtzConfig:
        # the edited file lives in the state dir: it cannot move it
        if os.path.abspath(cfg.server.state_dir) != os.path.abspath(self.state_dir):
            if cfg.path == self.override:
                cfg.warnings.append(f"state_dir is fixed to {self.state_dir} "
                                    "(the edited config lives there)")
            cfg.server.state_dir = self.state_dir
        return cfg

    # ------------------------------------------------------------ editing
    def validate(self, text: str) -> Tuple[Optional[PtzConfig], str]:
        """(config, "") if valid, (None, error message) otherwise."""
        os.makedirs(self.state_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(suffix=".cfg", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            return load(path), ""
        except (ConfigError, configparser.Error, ValueError) as e:
            return None, str(e).replace(path, "ptz.cfg")
        finally:
            os.remove(path)

    def save(self, text: str) -> List[str]:
        cfg, error = self.validate(text)
        if cfg is None:
            raise ConfigError(error)
        self._backup()
        tmp = self.override + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, self.override)
        return cfg.warnings

    def reset(self) -> None:
        """Back to the repository default (the edited file is backed up)."""
        if self.has_override:
            self._backup()
            os.remove(self.override)

    def _backup(self) -> None:
        if not os.path.exists(self.active):
            return
        os.makedirs(self.backup_dir, exist_ok=True)
        name = time.strftime("ptz-%Y%m%d-%H%M%S.cfg")
        with open(self.active, encoding="utf-8") as src, \
                open(os.path.join(self.backup_dir, name), "w", encoding="utf-8") as dst:
            dst.write(src.read())
        for old in self.backups()[MAX_BACKUPS:]:
            os.remove(os.path.join(self.backup_dir, old))

    def backups(self) -> List[str]:
        if not os.path.isdir(self.backup_dir):
            return []
        return sorted((f for f in os.listdir(self.backup_dir) if f.endswith(".cfg")),
                      reverse=True)

    def read_backup(self, name: str) -> str:
        if not re.fullmatch(r"ptz-\d{8}-\d{6}\.cfg", name):
            raise ConfigError("invalid backup name")
        with open(os.path.join(self.backup_dir, name), encoding="utf-8") as f:
            return f.read()

    def info(self) -> dict:
        return {"active": self.active, "default": self.default, "edited": self.override,
                "is_edited": self.has_override, "backups": self.backups(),
                "load_error": self.load_error}
