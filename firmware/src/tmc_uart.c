#include "tmc_uart.h"

#include "config.h"
#include "hardware/gpio.h"
#include "hardware/uart.h"
#include "pico/stdlib.h"
#include "protocol.h"

#define TMC_SYNC 0x05

static uart_inst_t *tmc;
static uint32_t byte_us;

/* RP2040: UART index for a pin, TX pins are 4n, RX pins are 4n+1 */
static int uart_index(uint8_t pin) { return ((pin + 4) / 8) % 2; }

int tmc_uart_init(uint8_t rx_pin, uint8_t tx_pin, uint32_t baud)
{
    if (rx_pin > 29 || tx_pin > 29 || (tx_pin % 4) != 0 || (rx_pin % 4) != 1)
        return ACK_BAD_ARG;
    int idx = uart_index(tx_pin);
    if (idx != uart_index(rx_pin))
        return ACK_BAD_ARG;
    uart_inst_t *u = uart_get_instance(idx);
    if (u == HOST_UART || baud < 9600 || baud > 500000)
        return ACK_BAD_ARG;
    uart_init(u, baud);
    uart_set_fifo_enabled(u, true);
    gpio_set_function(tx_pin, GPIO_FUNC_UART);
    gpio_set_function(rx_pin, GPIO_FUNC_UART);
    gpio_pull_up(rx_pin);
    byte_us = 10u * 1000000u / baud + 1;
    tmc = u;
    return ACK_OK;
}

bool tmc_uart_ready(void) { return tmc != NULL; }

static uint8_t crc8(const uint8_t *d, int n)
{
    uint8_t crc = 0;
    for (int i = 0; i < n; i++) {
        uint8_t b = d[i];
        for (int j = 0; j < 8; j++) {
            if ((crc >> 7) ^ (b & 1))
                crc = (uint8_t)((crc << 1) ^ 0x07);
            else
                crc = (uint8_t)(crc << 1);
            b >>= 1;
        }
    }
    return crc;
}

static void drain(void)
{
    while (uart_is_readable(tmc))
        (void)uart_getc(tmc);
}

/* Read up to max bytes, stopping when the line stays idle. */
static int read_burst(uint8_t *buf, int max)
{
    int n = 0;
    while (n < max && uart_is_readable_within_us(tmc, byte_us * 24))
        buf[n++] = (uint8_t)uart_getc(tmc);
    return n;
}

int tmc_write(uint8_t addr, uint8_t reg, uint32_t value)
{
    if (!tmc)
        return ACK_NOT_CONFIGURED;
    uint8_t d[8] = { TMC_SYNC, addr, (uint8_t)(reg | 0x80),
                     (uint8_t)(value >> 24), (uint8_t)(value >> 16),
                     (uint8_t)(value >> 8), (uint8_t)value, 0 };
    d[7] = crc8(d, 7);
    drain();
    uart_write_blocking(tmc, d, sizeof d);
    uint8_t echo[8];
    read_burst(echo, sizeof echo);          /* discard single-wire echo */
    busy_wait_us(byte_us * 4);              /* bus idle time between datagrams */
    return ACK_OK;
}

int tmc_read(uint8_t addr, uint8_t reg, uint32_t *value)
{
    if (!tmc)
        return ACK_NOT_CONFIGURED;
    uint8_t d[4] = { TMC_SYNC, addr, reg, 0 };
    d[3] = crc8(d, 3);
    drain();
    uart_write_blocking(tmc, d, sizeof d);

    /* echo (4 bytes, if single-wire) + reply (8 bytes) */
    uint8_t buf[16];
    int n = read_burst(buf, sizeof buf);
    for (int i = 0; i + 8 <= n; i++) {
        uint8_t *r = &buf[i];
        if (r[0] == TMC_SYNC && r[1] == 0xFF && r[2] == reg && crc8(r, 7) == r[7]) {
            *value = ((uint32_t)r[3] << 24) | ((uint32_t)r[4] << 16)
                   | ((uint32_t)r[5] << 8) | r[6];
            return ACK_OK;
        }
    }
    return ACK_TMC_ERROR;
}
