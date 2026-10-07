/* Host link: COBS + CRC16 framed messages over UART0 (RX by interrupt) */
#pragma once
#include <stddef.h>
#include <stdint.h>

typedef void (*frame_handler_t)(uint8_t id, uint8_t seq, const uint8_t *payload, size_t len);

void comm_init(void);
void comm_poll(frame_handler_t handler);    /* decode buffered bytes, call handler per frame */
void comm_send(uint8_t id, uint8_t seq, const void *payload, size_t len);
void comm_log(const char *fmt, ...);
uint32_t comm_rx_errors(void);
