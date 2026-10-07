import os
import random
import struct

import pytest

from ptz import protocol as P


@pytest.mark.parametrize("length", [0, 1, 5, 253, 254, 255, 300])
def test_cobs_roundtrip(length):
    rnd = random.Random(length)
    for _ in range(50):
        data = bytes(rnd.choice([0, 0, 1, 255, rnd.randrange(256)]) for _ in range(length))
        enc = P.cobs_encode(data)
        assert 0 not in enc
        assert P.cobs_decode(enc) == data


def test_crc_known_value():
    # CRC-16/CCITT-FALSE check value
    assert P.crc16_ccitt(b"123456789") == 0x29B1


def _sample(d: P.MsgDef):
    if d.fmt is None:
        return {"text": "hello"}
    codes = d.fmt.replace("4f", "ffff")
    sample = {"B": 7, "b": -1, "H": 1234, "I": 123456, "i": -98765, "f": 1.5}
    return {f: sample[c] for f, c in zip(d.fields, codes)}


@pytest.mark.parametrize("d", P.MESSAGES, ids=lambda d: d.name)
def test_encode_decode_all(d):
    fields = _sample(d)
    frame = P.encode(d.name, 42, **fields)
    assert frame[-1] == 0 and 0 not in frame[:-1]
    msgs = P.FrameDecoder().feed(frame)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.name == d.name and m.seq == 42
    assert m.fields == fields


def test_decoder_resyncs_after_garbage():
    dec = P.FrameDecoder()
    good = P.encode("PING", 3)
    corrupted = bytearray(P.encode("ACK", 1, acked_id=1, status=0))
    corrupted[2] ^= 0x55
    msgs = dec.feed(os.urandom(10).replace(b"\x00", b"\x01") + b"\x00"
                    + bytes(corrupted) + good)
    assert [m.name for m in msgs] == ["PING"]
    assert dec.errors >= 1


def test_frames_fit_max_size():
    for d in P.MESSAGES:
        if d.fmt is not None:
            assert 2 + struct.calcsize("<" + d.fmt) + 2 <= P.MAX_RAW, d.name
