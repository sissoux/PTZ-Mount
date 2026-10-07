"""Binary protocol between the Raspberry Pi host and the RP2040 firmware.

MUST stay in sync with firmware/src/protocol.h. See docs/protocol.md.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

PROTOCOL_VERSION = 1
MAX_AXES = 4
PIN_NONE = 0xFF
MAX_RAW = 64

# ---------------------------------------------------------------- constants
# CONFIG_AXIS flags
AXF_DIR_INVERT = 1 << 0
AXF_ENABLE_INVERT = 1 << 1
AXF_ENDSTOP_INVERT = 1 << 2
AXF_ENDSTOP_PULLUP = 1 << 3

# ACK status
ACK_OK = 0
ACK_STATUS = {
    0: "OK", 1: "UNKNOWN_CMD", 2: "BAD_LENGTH", 3: "BAD_ARG",
    4: "NOT_CONFIGURED", 5: "ESTOPPED", 6: "QUEUE_FULL", 7: "TMC_ERROR",
}

# STATUS sys flags
SYS_ESTOP = 1 << 0
SYS_WATCHDOG = 1 << 1
SYS_TMC_READY = 1 << 2

# STATUS axis flags
ST_ENABLED = 1 << 0
ST_MOVING = 1 << 1
ST_ENDSTOP = 1 << 2
ST_HOMING = 1 << 3
ST_AT_LIMIT = 1 << 4
ST_CONFIGURED = 1 << 5
ST_HALTED = 1 << 6
ST_POSITION_MODE = 1 << 7

# EVENT types
EV_MOVE_DONE = 1
EV_HOME_TRIGGERED = 2
EV_HOME_FAILED = 3
EV_LIMIT_HIT = 4
EV_ENDSTOP_HIT = 5
EV_WATCHDOG = 6
EV_BOOT = 7
EVENT_NAMES = {
    EV_MOVE_DONE: "MOVE_DONE", EV_HOME_TRIGGERED: "HOME_TRIGGERED",
    EV_HOME_FAILED: "HOME_FAILED", EV_LIMIT_HIT: "LIMIT_HIT",
    EV_ENDSTOP_HIT: "ENDSTOP_HIT", EV_WATCHDOG: "WATCHDOG", EV_BOOT: "BOOT",
}


# ---------------------------------------------------------------- messages
@dataclass(frozen=True)
class MsgDef:
    id: int
    name: str
    fmt: Optional[str]      # struct format without '<'; None = raw UTF-8 text
    fields: Tuple[str, ...]


_STATUS_FIELDS = ("time_ms", "sys_flags") + tuple(
    f"{k}{i}" for i in range(MAX_AXES) for k in ("pos", "vel", "flags"))

MESSAGES: List[MsgDef] = [
    # host -> mcu
    MsgDef(0x01, "PING", "", ()),
    MsgDef(0x02, "RESET", "", ()),
    MsgDef(0x03, "SET_STATUS_RATE", "H", ("rate_hz",)),
    MsgDef(0x04, "SET_WATCHDOG", "H", ("timeout_ms",)),
    MsgDef(0x10, "CONFIG_AXIS", "BBBBBBbfff",
           ("axis", "step_pin", "dir_pin", "enable_pin", "endstop_pin",
            "flags", "endstop_dir", "max_vel", "max_accel", "vel_accel")),
    MsgDef(0x11, "CONFIG_TMC_UART", "BBI", ("rx_pin", "tx_pin", "baud")),
    MsgDef(0x12, "TMC_WRITE", "BBI", ("addr", "reg", "value")),
    MsgDef(0x13, "TMC_READ", "BB", ("addr", "reg")),
    MsgDef(0x14, "SET_LIMITS", "Biib", ("axis", "min", "max", "enabled")),
    MsgDef(0x20, "ENABLE", "BB", ("axis_mask", "enable_mask")),
    MsgDef(0x21, "SET_VELOCITY", "B4f", ("axis_mask", "v0", "v1", "v2", "v3")),
    MsgDef(0x22, "MOVE_TO", "Biff", ("axis", "target", "max_vel", "accel")),
    MsgDef(0x23, "STOP", "B", ("axis_mask",)),
    MsgDef(0x24, "ESTOP", "", ()),
    MsgDef(0x25, "HOME", "Bfi", ("axis", "velocity", "max_travel")),
    MsgDef(0x26, "SET_POSITION", "Bi", ("axis", "position")),
    MsgDef(0x27, "KEEPALIVE", "", ()),
    MsgDef(0x28, "CLEAR_ESTOP", "", ()),
    # mcu -> host
    MsgDef(0x80, "ACK", "BB", ("acked_id", "status")),
    MsgDef(0x81, "PONG", "HHI", ("protocol_version", "max_axes", "uptime_ms")),
    MsgDef(0x82, "STATUS", "IB" + "ifB" * MAX_AXES, _STATUS_FIELDS),
    MsgDef(0x83, "EVENT", "BBi", ("type", "axis", "value")),
    MsgDef(0x84, "TMC_VALUE", "BBIB", ("addr", "reg", "value", "ok")),
    MsgDef(0x85, "LOG", None, ("text",)),
]
BY_ID: Dict[int, MsgDef] = {m.id: m for m in MESSAGES}
BY_NAME: Dict[str, MsgDef] = {m.name: m for m in MESSAGES}

#: Requests that the MCU answers (with ACK unless stated otherwise).
REPLY_FOR = {
    "PING": "PONG", "TMC_READ": "TMC_VALUE",
}
NO_REPLY = {"SET_VELOCITY", "KEEPALIVE"}


@dataclass
class Message:
    name: str
    seq: int = 0
    fields: Dict[str, object] = field(default_factory=dict)

    def __getitem__(self, key):
        return self.fields[key]

    @property
    def id(self) -> int:
        return BY_NAME[self.name].id


# ---------------------------------------------------------------- CRC / COBS
def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])
    code_idx, code = 0, 1
    for b in data:
        if b == 0:
            out[code_idx] = code
            code_idx, code = len(out), 1
            out.append(0)
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_idx] = code
                code_idx, code = len(out), 1
                out.append(0)
    out[code_idx] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise ValueError("zero byte inside COBS frame")
        i += 1
        end = i + code - 1
        if end > n:
            raise ValueError("truncated COBS frame")
        out += data[i:end]
        i = end
        if code < 0xFF and i < n:
            out.append(0)
    return bytes(out)


# ---------------------------------------------------------------- encode / decode
def pack_payload(name: str, **fields) -> bytes:
    d = BY_NAME[name]
    if d.fmt is None:
        return str(fields.get("text", "")).encode("utf-8")[:MAX_RAW - 4]
    values = [fields[f] for f in d.fields]
    return struct.pack("<" + d.fmt, *values)


def encode(name: str, seq: int = 0, **fields) -> bytes:
    """Build a complete wire frame (COBS encoded, 0x00 terminated)."""
    d = BY_NAME[name]
    raw = bytes([d.id, seq & 0xFF]) + pack_payload(name, **fields)
    if len(raw) + 2 > MAX_RAW:
        raise ValueError(f"{name}: frame too long")
    raw += struct.pack("<H", crc16_ccitt(raw))
    return cobs_encode(raw) + b"\x00"


def decode_raw(raw: bytes) -> Message:
    """Decode an already COBS-decoded frame."""
    if len(raw) < 4:
        raise ValueError("frame too short")
    body, crc = raw[:-2], struct.unpack("<H", raw[-2:])[0]
    if crc16_ccitt(body) != crc:
        raise ValueError("bad CRC")
    msg_id, seq, payload = body[0], body[1], body[2:]
    d = BY_ID.get(msg_id)
    if d is None:
        raise ValueError(f"unknown message id 0x{msg_id:02x}")
    if d.fmt is None:
        return Message(d.name, seq, {"text": payload.decode("utf-8", "replace")})
    size = struct.calcsize("<" + d.fmt)
    if len(payload) != size:
        raise ValueError(f"{d.name}: bad payload length {len(payload)} != {size}")
    values = struct.unpack("<" + d.fmt, payload)
    return Message(d.name, seq, dict(zip(d.fields, values)))


class FrameDecoder:
    """Incremental stream decoder: feed() bytes, get complete Messages."""

    def __init__(self):
        self._buf = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> List[Message]:
        msgs = []
        for b in data:
            if b == 0:
                if self._buf:
                    try:
                        msgs.append(decode_raw(cobs_decode(bytes(self._buf))))
                    except ValueError:
                        self.errors += 1
                    self._buf.clear()
            else:
                self._buf.append(b)
                if len(self._buf) > MAX_RAW * 2:   # garbage, resync
                    self._buf.clear()
                    self.errors += 1
        return msgs


def status_axes(msg: Message) -> List[Tuple[int, float, int]]:
    """Return [(pos, vel, flags), ...] for a STATUS message."""
    f = msg.fields
    return [(f[f"pos{i}"], f[f"vel{i}"], f[f"flags{i}"]) for i in range(MAX_AXES)]
