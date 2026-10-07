#include "comm.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "cobs.h"
#include "config.h"
#include "hardware/gpio.h"
#include "hardware/irq.h"
#include "hardware/uart.h"
#include "protocol.h"

#define RX_RING 1024

static volatile uint8_t ring[RX_RING];
static volatile uint16_t ring_head, ring_tail;
static uint8_t frame[MAX_RAW * 2];
static size_t frame_len;
static bool frame_overflow;
static uint32_t rx_errors;

static void on_uart_rx(void)
{
    while (uart_is_readable(HOST_UART)) {
        uint8_t c = (uint8_t)uart_getc(HOST_UART);
        uint16_t next = (uint16_t)((ring_head + 1) % RX_RING);
        if (next != ring_tail) {
            ring[ring_head] = c;
            ring_head = next;
        } else {
            rx_errors++;
        }
    }
}

void comm_init(void)
{
    uart_init(HOST_UART, HOST_UART_BAUD);
    gpio_set_function(HOST_UART_TX_PIN, GPIO_FUNC_UART);
    gpio_set_function(HOST_UART_RX_PIN, GPIO_FUNC_UART);
    uart_set_hw_flow(HOST_UART, false, false);
    uart_set_format(HOST_UART, 8, 1, UART_PARITY_NONE);
    uart_set_fifo_enabled(HOST_UART, true);
    irq_set_exclusive_handler(HOST_UART_IRQ, on_uart_rx);
    irq_set_enabled(HOST_UART_IRQ, true);
    uart_set_irq_enables(HOST_UART, true, false);
}

uint32_t comm_rx_errors(void) { return rx_errors; }

static void handle_frame(frame_handler_t handler)
{
    uint8_t raw[MAX_RAW];
    size_t n = cobs_decode(frame, frame_len, raw, sizeof raw);
    if (n < 4) {
        rx_errors++;
        return;
    }
    uint16_t crc = (uint16_t)(raw[n - 2] | (raw[n - 1] << 8));
    if (crc16_ccitt(raw, n - 2) != crc) {
        rx_errors++;
        return;
    }
    handler(raw[0], raw[1], &raw[2], n - 4);
}

void comm_poll(frame_handler_t handler)
{
    while (ring_tail != ring_head) {
        uint8_t c = ring[ring_tail];
        ring_tail = (uint16_t)((ring_tail + 1) % RX_RING);
        if (c == 0) {
            if (frame_len && !frame_overflow)
                handle_frame(handler);
            frame_len = 0;
            frame_overflow = false;
        } else if (frame_len < sizeof frame) {
            frame[frame_len++] = c;
        } else {
            frame_overflow = true;
        }
    }
}

void comm_send(uint8_t id, uint8_t seq, const void *payload, size_t len)
{
    uint8_t raw[MAX_RAW];
    uint8_t enc[MAX_RAW + 4];
    if (len + 4 > MAX_RAW)
        return;
    raw[0] = id;
    raw[1] = seq;
    memcpy(&raw[2], payload, len);
    uint16_t crc = crc16_ccitt(raw, len + 2);
    raw[len + 2] = (uint8_t)crc;
    raw[len + 3] = (uint8_t)(crc >> 8);
    size_t n = cobs_encode(raw, len + 4, enc);
    enc[n++] = 0;
    uart_write_blocking(HOST_UART, enc, n);
}

void comm_log(const char *fmt, ...)
{
    char buf[MAX_RAW - 4];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    if (n < 0)
        return;
    if ((size_t)n >= sizeof buf)
        n = sizeof buf - 1;
    comm_send(MSG_LOG, 0, buf, (size_t)n);
}
