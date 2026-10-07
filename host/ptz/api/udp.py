"""Low-latency JSON-over-UDP control.

Intended for hardware joysticks / dedicated software on the LAN. One JSON
command per datagram (same commands as the WebSocket, see commands.py).

    {"cmd": "jog", "pan": 0.3, "tilt": 0, "zoom": -1}

Jog commands must be repeated at least every motion.jog_timeout (typically
every 20-50 ms while the stick is deflected).

Send {"cmd": "subscribe"} every few seconds to receive status datagrams.
Replies are only sent for commands carrying an "id" or on error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, Tuple

from ..motion import MotionController
from .commands import dispatch

log = logging.getLogger("ptz.udp")
SUBSCRIPTION_TTL = 10.0


class UdpServer(asyncio.DatagramProtocol):
    def __init__(self, ctrl: MotionController, host: str, port: int):
        self.ctrl, self.host, self.port = ctrl, host, port
        self.transport = None
        self.subscribers: Dict[Tuple[str, int], float] = {}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, local_addr=(self.host, self.port))
        log.info("UDP control on port %d", self.port)

    async def close(self) -> None:
        if self.transport:
            self.transport.close()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if isinstance(msg, dict) and msg.get("cmd") == "jog":
            self.ctrl.jog({k: v for k, v in msg.items() if k in self.ctrl.state})
            return                                   # hot path: no task, no reply
        if isinstance(msg, dict) and msg.get("cmd") == "subscribe":
            self.subscribers[addr] = time.monotonic() + SUBSCRIPTION_TTL
            return
        asyncio.get_running_loop().create_task(self._handle(msg, addr))

    async def _handle(self, msg, addr) -> None:
        reply = await dispatch(self.ctrl, msg, source=f"udp {addr[0]}")
        if (isinstance(msg, dict) and "id" in msg) or not reply["ok"]:
            if isinstance(msg, dict):
                reply["id"] = msg.get("id")
            self.transport.sendto(json.dumps(reply).encode(), addr)

    async def broadcast(self, payload: dict) -> None:
        if not self.subscribers or not self.transport:
            return
        now = time.monotonic()
        data = json.dumps(payload).encode()
        for addr, expiry in list(self.subscribers.items()):
            if expiry < now:
                del self.subscribers[addr]
            else:
                self.transport.sendto(data, addr)
