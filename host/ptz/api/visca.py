"""VISCA over IP (Sony) front-end.

Lets off-the-shelf PTZ keyboards/joysticks and software (OBS PTZ plugin,
vMix, Bitfocus Companion...) drive the head. Accepts both the Sony
VISCA-over-IP framing (8-byte header) and raw VISCA datagrams.

Supported subset:
  81 01 06 01 VV WW XX YY FF   Pan/tilt drive (incl. stop)
  81 01 06 02 VV WW 0Y0Y0Y0Y 0Z0Z0Z0Z FF   Absolute pan/tilt (0.1 deg units)
  81 01 06 04 FF               Home  -> go to pan 0 / tilt 0
  81 01 06 05 FF               Reset -> run the homing sequence
  81 01 04 07 XX FF            Zoom stop / tele / wide (std and variable)
  81 01 04 47 0p0q0r0s FF      Zoom direct (0x0000..0x4000 = zoom min..max)
  81 01 04 3F 0X PP FF         Preset reset / set / recall
  81 09 06 12 FF               Pan/tilt position inquiry
  81 09 04 47 FF               Zoom position inquiry

VISCA drive commands are "move until stop", so the current drive state is
re-sent to the motion controller periodically (the MCU watchdog would stop
it otherwise).
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Dict, Optional

from ..motion import MotionController

log = logging.getLogger("ptz.visca")

T_COMMAND, T_INQUIRY, T_REPLY = 0x0100, 0x0110, 0x0111
T_CONTROL, T_CONTROL_REPLY = 0x0200, 0x0201

PAN_SPEED_MAX = 0x18
TILT_SPEED_MAX = 0x14
ZOOM_SPEED_MAX = 7
DIRECT_ZOOM_MAX = 0x4000
POS_SCALE = 10.0                  # VISCA position unit = 0.1 deg

ACK = bytes([0x90, 0x41, 0xFF])
COMPLETION = bytes([0x90, 0x51, 0xFF])
ERR_SYNTAX = bytes([0x90, 0x60, 0x02, 0xFF])
ERR_NOT_EXEC = bytes([0x90, 0x61, 0x41, 0xFF])


def _nibbles_to_int(nibbles: bytes, signed: bool = True) -> int:
    v = 0
    for n in nibbles:
        v = (v << 4) | (n & 0x0F)
    bits = 4 * len(nibbles)
    if signed and v >= 1 << (bits - 1):
        v -= 1 << bits
    return v


def _int_to_nibbles(v: int, count: int = 4) -> bytes:
    v &= (1 << (4 * count)) - 1
    return bytes((v >> (4 * (count - 1 - i))) & 0x0F for i in range(count))


class ViscaServer(asyncio.DatagramProtocol):
    def __init__(self, ctrl: MotionController, host: str, port: int):
        self.ctrl, self.host, self.port = ctrl, host, port
        self.transport = None
        self.drive: Dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, local_addr=(self.host, self.port))
        self._task = loop.create_task(self._hold_loop())
        log.info("VISCA over IP on UDP port %d", self.port)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self.transport:
            self.transport.close()

    def connection_made(self, transport):
        self.transport = transport

    async def _hold_loop(self) -> None:
        period = max(0.05, self.ctrl.cfg.motion.jog_timeout / 3)
        while True:
            await asyncio.sleep(period)
            if any(self.drive.values()):
                self.ctrl.jog(self.drive)

    def _set_drive(self, **values: float) -> None:
        self.drive.update(values)
        self.ctrl.jog(values)

    # ------------------------------------------------------------ rx
    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) >= 8 and data[0] in (0x01, 0x02):
            ptype, length, seq = struct.unpack(">HHI", data[:8])
            payload = data[8:8 + length]
            if ptype == T_CONTROL:
                self._send(addr, T_CONTROL_REPLY, seq, bytes([0x01]))
                return
            replies = self._handle(payload)
            for r in replies:
                self._send(addr, T_REPLY, seq, r)
        elif data[:1] and data[0] & 0xF0 == 0x80:
            for r in self._handle(data):
                self.transport.sendto(r, addr)

    def _send(self, addr, ptype: int, seq: int, payload: bytes) -> None:
        self.transport.sendto(struct.pack(">HHI", ptype, len(payload), seq) + payload, addr)

    # ------------------------------------------------------------ commands
    def _handle(self, p: bytes):
        if len(p) < 3 or p[-1] != 0xFF:
            return [ERR_SYNTAX]
        c = self.ctrl
        try:
            # ---- inquiries
            if p[1] == 0x09:
                if p[2:4] == bytes([0x06, 0x12]):
                    pan = int(c.position("pan") * POS_SCALE) if "pan" in c.state else 0
                    tilt = int(c.position("tilt") * POS_SCALE) if "tilt" in c.state else 0
                    return [bytes([0x90, 0x50]) + _int_to_nibbles(pan)
                            + _int_to_nibbles(tilt) + b"\xFF"]
                if p[2:4] == bytes([0x04, 0x47]) and "zoom" in c.state:
                    ax = c.axis("zoom")
                    frac = (c.position("zoom") - ax.position_min) / (ax.position_max - ax.position_min)
                    val = int(max(0.0, min(1.0, frac)) * DIRECT_ZOOM_MAX)
                    return [bytes([0x90, 0x50]) + _int_to_nibbles(val) + b"\xFF"]
                return [ERR_SYNTAX]
            if p[1] != 0x01:
                return [ERR_SYNTAX]
            cat, cmd = p[2], p[3]
            # ---- pan / tilt
            if cat == 0x06 and cmd == 0x01 and len(p) == 9:
                vv, ww, xx, yy = p[4], p[5], p[6], p[7]
                pan = {1: -1.0, 2: 1.0}.get(xx, 0.0) * min(vv, PAN_SPEED_MAX) / PAN_SPEED_MAX
                tilt = {1: 1.0, 2: -1.0}.get(yy, 0.0) * min(ww, TILT_SPEED_MAX) / TILT_SPEED_MAX
                self._set_drive(pan=pan, tilt=tilt)
                return [ACK, COMPLETION]
            if cat == 0x06 and cmd == 0x02 and len(p) == 15:
                speed = max(p[4] / PAN_SPEED_MAX, p[5] / TILT_SPEED_MAX)
                pan = _nibbles_to_int(p[6:10]) / POS_SCALE
                tilt = _nibbles_to_int(p[10:14]) / POS_SCALE
                self._spawn(c.goto({"pan": pan, "tilt": tilt}, speed=min(1.0, speed)))
                return [ACK, COMPLETION]
            if cat == 0x06 and cmd == 0x04:
                self._spawn(c.goto({k: 0.0 for k in ("pan", "tilt") if k in c.state}))
                return [ACK, COMPLETION]
            if cat == 0x06 and cmd == 0x05:
                self._spawn(c.home())
                return [ACK, COMPLETION]
            # ---- zoom
            if cat == 0x04 and cmd == 0x07 and len(p) == 6:
                z = p[4]
                if z == 0x00:
                    v = 0.0
                elif z == 0x02:
                    v = 0.5
                elif z == 0x03:
                    v = -0.5
                elif z & 0xF0 == 0x20:
                    v = ((z & 0x0F) + 1) / (ZOOM_SPEED_MAX + 1)
                elif z & 0xF0 == 0x30:
                    v = -((z & 0x0F) + 1) / (ZOOM_SPEED_MAX + 1)
                else:
                    return [ERR_SYNTAX]
                self._set_drive(zoom=v)
                return [ACK, COMPLETION]
            if cat == 0x04 and cmd == 0x47 and len(p) == 9 and "zoom" in c.state:
                ax = c.axis("zoom")
                frac = _nibbles_to_int(p[4:8], signed=False) / DIRECT_ZOOM_MAX
                self._spawn(c.goto({"zoom": ax.position_min
                                    + frac * (ax.position_max - ax.position_min)}))
                return [ACK, COMPLETION]
            # ---- presets
            if cat == 0x04 and cmd == 0x3F and len(p) == 7:
                op, pid = p[4], p[5]
                if op == 0x00:
                    c.presets.delete(pid)
                elif op == 0x01:
                    c.save_preset(pid)
                elif op == 0x02:
                    self._spawn(c.recall_preset(pid))
                else:
                    return [ERR_SYNTAX]
                return [ACK, COMPLETION]
        except Exception as e:  # noqa: BLE001
            log.warning("VISCA command %s failed: %s", p.hex(" "), e)
            return [ERR_NOT_EXEC]
        log.debug("unsupported VISCA command %s", p.hex(" "))
        return [ERR_SYNTAX]

    def _spawn(self, coro) -> None:
        async def run():
            try:
                await coro
            except Exception as e:  # noqa: BLE001
                log.warning("VISCA: %s", e)
        asyncio.get_running_loop().create_task(run())
