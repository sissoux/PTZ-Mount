/* COBS framing and CRC16-CCITT (identical to host/ptz/protocol.py) */
#pragma once
#include <stddef.h>
#include <stdint.h>

/* out must hold len + len/254 + 1 bytes. Returns encoded length. */
size_t cobs_encode(const uint8_t *in, size_t len, uint8_t *out);

/* Returns decoded length, 0 on malformed input or overflow. */
size_t cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t out_max);

/* CRC16-CCITT, poly 0x1021, init 0xFFFF */
uint16_t crc16_ccitt(const uint8_t *data, size_t len);
