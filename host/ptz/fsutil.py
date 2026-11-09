"""Power-cut safe file writes.

The Pi may be unplugged at any time (and has already lost an SD card).
Every state file is written to a temporary file, flushed to the card, then
renamed over the old one: after a power cut the file is either the old or
the new version, never empty or half written.
"""
from __future__ import annotations

import json
import os


def atomic_write_text(path: str, text: str) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    if hasattr(os, "O_DIRECTORY"):            # make the rename itself durable (POSIX)
        fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write_json(path: str, data, indent=None) -> None:
    atomic_write_text(path, json.dumps(data, indent=indent))


def sync_all() -> None:
    """Flush every pending write of the system to the storage."""
    if hasattr(os, "sync"):
        os.sync()
