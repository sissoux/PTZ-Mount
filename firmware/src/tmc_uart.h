/*
 * Minimal TMC2209 UART access. The firmware only moves raw register values;
 * all register computation is done by the host (host/ptz/tmc2209.py).
 *
 * On the SKR Pico the four drivers share one single-wire bus
 * (TX = GPIO8 through a resistor, RX = GPIO9 => hardware UART1).
 * Every byte sent is echoed back and is discarded.
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>

int  tmc_uart_init(uint8_t rx_pin, uint8_t tx_pin, uint32_t baud);   /* ACK_* code */
bool tmc_uart_ready(void);
int  tmc_write(uint8_t addr, uint8_t reg, uint32_t value);           /* ACK_* code */
int  tmc_read(uint8_t addr, uint8_t reg, uint32_t *value);           /* ACK_* code */
