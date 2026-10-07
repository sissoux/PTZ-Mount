/*
 * PTZ motion firmware for the RP2040 (BigTreeTech SKR Pico).
 *
 *   core 0 : host link (UART0), command decoding, TMC2209 UART, status/events
 *   core 1 : real-time motion engine (step ISR + 1 kHz control loop)
 *
 * The firmware is board-agnostic: every pin is sent by the host at startup
 * from config/ptz.cfg. Only the host UART pins are fixed (config.h).
 */
#include <stdio.h>

#include "comm.h"
#include "commands.h"
#include "config.h"
#include "hardware/watchdog.h"
#include "pico/multicore.h"
#include "pico/stdlib.h"
#include "protocol.h"
#include "stepper.h"
#include "tmc_uart.h"

int main(void)
{
    stdio_init_all();                       /* USB CDC for debug printf only */
    bool wd_reboot = watchdog_caused_reboot();

    stepper_init();
    multicore_launch_core1(stepper_core1_main);
    comm_init();
    watchdog_enable(HW_WATCHDOG_MS, true);

    msg_event_t boot = { .type = EV_BOOT, .axis = 0, .value = wd_reboot ? 1 : 0 };
    comm_send(MSG_EVENT, 0, &boot, sizeof boot);
    if (wd_reboot)
        comm_log("rebooted by hardware watchdog");

    uint32_t last_hb = stepper_heartbeat();
    absolute_time_t next_status = get_absolute_time();

    while (true) {
        comm_poll(commands_handle);

        msg_event_t ev;
        while (stepper_pop_event(&ev))
            comm_send(MSG_EVENT, 0, &ev, sizeof ev);

        if (time_reached(next_status)) {
            next_status = delayed_by_us(get_absolute_time(), g_status_period_us);
            msg_status_t st;
            stepper_get_status(&st);
            if (tmc_uart_ready())
                st.sys_flags |= SYS_TMC_READY;
            comm_send(MSG_STATUS, 0, &st, sizeof st);
        }

        /* feed the hardware watchdog only while core 1 is alive */
        uint32_t hb = stepper_heartbeat();
        if (hb != last_hb) {
            last_hb = hb;
            watchdog_update();
        }
    }
}
