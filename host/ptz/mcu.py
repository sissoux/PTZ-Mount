"""Link to the RP2040 firmware: transport + request/reply handling."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Callable, Dict, Optional

from . import protocol as P

log = logging.getLogger("ptz.mcu")


class McuError(Exception):
    pass


# ---------------------------------------------------------------- transports
class SerialTransport:
    """pyserial based transport.

    On Linux the serial fd is registered in the asyncio loop (no thread, lowest
    latency). Elsewhere a reader thread is used.
    """

    def __init__(self, port: str, baud: int):
        self.port, self.baud = port, baud
        self._ser = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    async def open(self, on_data: Callable[[bytes], None]) -> None:
        import serial  # pyserial
        self._ser = serial.Serial(self.port, self.baud, timeout=0)
        self._ser.reset_input_buffer()
        loop = asyncio.get_running_loop()
        self._running = True
        if os.name == "posix":
            def _readable():
                data = self._ser.read(self._ser.in_waiting or 1)
                if data:
                    on_data(data)
            loop.add_reader(self._ser.fileno(), _readable)
        else:
            self._ser.timeout = 0.01

            def _reader():
                while self._running:
                    data = self._ser.read(self._ser.in_waiting or 1)
                    if data:
                        loop.call_soon_threadsafe(on_data, data)
            self._thread = threading.Thread(target=_reader, daemon=True)
            self._thread.start()

    def write(self, data: bytes) -> None:
        self._ser.write(data)

    async def close(self) -> None:
        self._running = False
        if self._ser is not None:
            if os.name == "posix":
                try:
                    asyncio.get_running_loop().remove_reader(self._ser.fileno())
                except Exception:
                    pass
            self._ser.close()


# ---------------------------------------------------------------- link
class McuLink:
    def __init__(self, transport):
        self.transport = transport
        self._decoder = P.FrameDecoder()
        self._pending: Dict[int, asyncio.Future] = {}
        self._seq = 0
        self.on_status: Optional[Callable[[P.Message], None]] = None
        self.on_event: Optional[Callable[[P.Message], None]] = None
        self.last_rx = 0.0

    async def start(self) -> None:
        await self.transport.open(self._on_data)

    async def close(self) -> None:
        await self.transport.close()

    @property
    def rx_errors(self) -> int:
        return self._decoder.errors

    # ------------------------------------------------------------ rx
    def _on_data(self, data: bytes) -> None:
        self.last_rx = asyncio.get_event_loop().time()
        for msg in self._decoder.feed(data):
            if msg.name in ("ACK", "PONG", "TMC_VALUE"):
                fut = self._pending.pop(msg.seq, None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
            elif msg.name == "STATUS":
                if self.on_status:
                    self.on_status(msg)
            elif msg.name == "EVENT":
                log.debug("event %s axis=%d value=%d",
                          P.EVENT_NAMES.get(msg["type"], msg["type"]),
                          msg["axis"], msg["value"])
                if self.on_event:
                    self.on_event(msg)
            elif msg.name == "LOG":
                log.info("mcu: %s", msg["text"])

    # ------------------------------------------------------------ tx
    def _next_seq(self) -> int:
        self._seq = self._seq % 255 + 1      # 1..255, 0 = unsolicited
        return self._seq

    def send(self, name: str, **fields) -> None:
        """Fire-and-forget (used for the SET_VELOCITY stream)."""
        self.transport.write(P.encode(name, 0, **fields))

    async def request(self, name: str, timeout: float = 0.3, retries: int = 2,
                      **fields) -> P.Message:
        last_exc: Exception = McuError(f"{name}: no reply")
        for _ in range(retries + 1):
            seq = self._next_seq()
            fut = asyncio.get_running_loop().create_future()
            self._pending[seq] = fut
            self.transport.write(P.encode(name, seq, **fields))
            try:
                reply = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                self._pending.pop(seq, None)
                last_exc = McuError(f"{name}: timeout")
                continue
            if reply.name == "ACK" and reply["status"] != P.ACK_OK:
                raise McuError(f"{name}: {P.ACK_STATUS.get(reply['status'], reply['status'])}")
            return reply
        raise last_exc
