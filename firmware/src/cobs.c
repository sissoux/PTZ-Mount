#include "cobs.h"

size_t cobs_encode(const uint8_t *in, size_t len, uint8_t *out)
{
    size_t code_idx = 0, o = 1;
    uint8_t code = 1;
    for (size_t i = 0; i < len; i++) {
        if (in[i] == 0) {
            out[code_idx] = code;
            code_idx = o++;
            code = 1;
        } else {
            out[o++] = in[i];
            if (++code == 0xFF) {
                out[code_idx] = code;
                code_idx = o++;
                code = 1;
            }
        }
    }
    out[code_idx] = code;
    return o;
}

size_t cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t out_max)
{
    size_t i = 0, o = 0;
    while (i < len) {
        uint8_t code = in[i++];
        if (code == 0)
            return 0;
        for (uint8_t j = 1; j < code; j++) {
            if (i >= len || o >= out_max)
                return 0;
            out[o++] = in[i++];
        }
        if (code < 0xFF && i < len) {
            if (o >= out_max)
                return 0;
            out[o++] = 0;
        }
    }
    return o;
}

uint16_t crc16_ccitt(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
    return crc;
}
