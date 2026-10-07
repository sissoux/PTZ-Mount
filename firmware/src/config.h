/*
 * Compile-time firmware settings.
 *
 * Everything axis-related (pins, speeds, limits, driver currents) is sent by
 * the host at runtime from config/ptz.cfg. Only what the firmware needs
 * before the host talks to it lives here.
 */
#pragma once

/* Host link: SKR Pico "Raspberry Pi" header = UART0 on GPIO0 (TX) / GPIO1 (RX) */
#define HOST_UART           uart0
#define HOST_UART_IRQ       UART0_IRQ
#define HOST_UART_TX_PIN    0
#define HOST_UART_RX_PIN    1
#ifndef HOST_UART_BAUD
#define HOST_UART_BAUD      500000      /* must match [mcu] baud in ptz.cfg */
#endif

/* Step generation: fixed-rate ISR on core 1 (DDS / phase accumulator).
 * Max step rate = STEP_TICK_HZ / 2 per axis. */
#define STEP_TICK_HZ        40000

/* Velocity / position control loop (ramps, limits, homing) on core 1 */
#define CONTROL_HZ          1000

/* Defaults until the host configures them */
#define DEFAULT_STATUS_HZ   50
#define DEFAULT_WATCHDOG_MS 300

/* Hardware watchdog: reboot if core 1 stops running */
#define HW_WATCHDOG_MS      200

#define CMD_QUEUE_DEPTH     32
#define EVENT_QUEUE_DEPTH   16
