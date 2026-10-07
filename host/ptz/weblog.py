"""Log capture for the web debug console.

A logging handler keeps the last messages in memory and forwards new ones to
subscribers (WebSocket clients with the debug console open). It is safe to
log from other threads (serial reader thread on Windows).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Callable, Deque, List, Optional

BACKLOG = 400


class WebLogHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.buffer: Deque[dict] = deque(maxlen=BACKLOG)
        self.listeners: List[Callable[[dict], None]] = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "aiohttp.access":
            return
        if record.levelno < logging.INFO and not record.name.startswith("ptz"):
            return                              # third-party debug noise
        try:
            entry = {"t": record.created, "level": record.levelname,
                     "name": record.name, "msg": self.format(record)}
        except Exception:  # noqa: BLE001
            return
        self.buffer.append(entry)
        if not self.listeners or self.loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for cb in list(self.listeners):
            if running is self.loop:
                cb(entry)
            else:
                self.loop.call_soon_threadsafe(cb, entry)


_handler: Optional[WebLogHandler] = None


def install() -> WebLogHandler:
    """Attach the handler to the root logger (idempotent)."""
    global _handler
    if _handler is None:
        _handler = WebLogHandler()
        logging.getLogger().addHandler(_handler)
    return _handler


def set_debug(on: bool) -> None:
    """Debug mode: verbose 'ptz' loggers (MCU events, every command...)."""
    logging.getLogger("ptz").setLevel(logging.DEBUG if on else logging.INFO)
    root = logging.getLogger()
    if on and root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)
    for h in root.handlers:                     # keep the journal at INFO
        if not isinstance(h, WebLogHandler):
            h.setLevel(logging.INFO)


def is_debug() -> bool:
    return logging.getLogger("ptz").getEffectiveLevel() <= logging.DEBUG
