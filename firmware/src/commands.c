/*
 * Core 0: decode host messages, validate them, forward motion commands to
 * core 1 through the command queue, answer with ACK / replies.
 */
#include "commands.h"

#include <string.h>

#include "comm.h"
#include "config.h"
#include "pico/stdlib.h"
#include "protocol.h"
#include "stepper.h"
#include "tmc_uart.h"

uint32_t g_status_period_us = 1000000 / DEFAULT_STATUS_HZ;

/* core 0 mirror of what has been configured, for argument validation */
static bool configured[MAX_AXES];
static bool has_endstop[MAX_AXES];
static bool estop;

static void ack(uint8_t id, uint8_t seq, uint8_t status)
{
    msg_ack_t a = { .acked_id = id, .status = status };
    comm_send(MSG_ACK, seq, &a, sizeof a);
}

static uint8_t push(const stepper_cmd_t *c)
{
    return stepper_push(c) ? ACK_OK : ACK_QUEUE_FULL;
}

static bool axis_ok(uint8_t axis) { return axis < MAX_AXES && configured[axis]; }

/* Copy payload into a typed struct, or reply BAD_LENGTH and return. */
#define DECODE(type, var)                         \
    type var;                                     \
    if (len != sizeof(type)) {                    \
        ack(id, seq, ACK_BAD_LENGTH);             \
        return;                                   \
    }                                             \
    memcpy(&var, p, sizeof(type))

void commands_handle(uint8_t id, uint8_t seq, const uint8_t *p, size_t len)
{
    stepper_cmd_t c;
    memset(&c, 0, sizeof c);

    switch (id) {
    case MSG_PING: {
        msg_pong_t r = { .protocol_version = PROTOCOL_VERSION, .max_axes = MAX_AXES,
                         .uptime_ms = to_ms_since_boot(get_absolute_time()) };
        comm_send(MSG_PONG, seq, &r, sizeof r);
        return;
    }
    case MSG_RESET:
        memset(configured, 0, sizeof configured);
        memset(has_endstop, 0, sizeof has_endstop);
        estop = false;
        c.type = CMD_RESET;
        ack(id, seq, push(&c));
        return;

    case MSG_SET_STATUS_RATE: {
        DECODE(msg_set_status_rate_t, m);
        if (m.rate_hz < 1 || m.rate_hz > 500) {
            ack(id, seq, ACK_BAD_ARG);
            return;
        }
        g_status_period_us = 1000000u / m.rate_hz;
        ack(id, seq, ACK_OK);
        return;
    }
    case MSG_SET_WATCHDOG: {
        DECODE(msg_set_watchdog_t, m);
        c.type = CMD_SET_WATCHDOG;
        c.i1 = m.timeout_ms;
        ack(id, seq, push(&c));
        return;
    }
    case MSG_CONFIG_AXIS: {
        DECODE(msg_config_axis_t, m);
        bool pins_ok = m.step_pin <= 29 && m.dir_pin <= 29 && m.step_pin != m.dir_pin
                    && (m.enable_pin <= 29 || m.enable_pin == PIN_NONE)
                    && (m.endstop_pin <= 29 || m.endstop_pin == PIN_NONE)
                    && m.step_pin != HOST_UART_TX_PIN && m.step_pin != HOST_UART_RX_PIN;
        if (m.axis >= MAX_AXES || !pins_ok || m.max_vel <= 0 || m.max_accel <= 0) {
            ack(id, seq, ACK_BAD_ARG);
            return;
        }
        c.type = CMD_CONFIG_AXIS;
        c.axis = m.axis;
        c.cfg = m;
        uint8_t st = push(&c);
        if (st == ACK_OK) {
            configured[m.axis] = true;
            has_endstop[m.axis] = m.endstop_pin != PIN_NONE;
        }
        ack(id, seq, st);
        return;
    }
    case MSG_CONFIG_TMC_UART: {
        DECODE(msg_config_tmc_uart_t, m);
        ack(id, seq, (uint8_t)tmc_uart_init(m.rx_pin, m.tx_pin, m.baud));
        return;
    }
    case MSG_TMC_WRITE: {
        DECODE(msg_tmc_write_t, m);
        ack(id, seq, (uint8_t)tmc_write(m.addr, m.reg, m.value));
        return;
    }
    case MSG_TMC_READ: {
        DECODE(msg_tmc_read_t, m);
        uint32_t value = 0;                 /* never point into a packed struct (M0+ faults) */
        bool ok = tmc_read(m.addr, m.reg, &value) == ACK_OK;
        msg_tmc_value_t r = { .addr = m.addr, .reg = m.reg, .value = value, .ok = ok };
        comm_send(MSG_TMC_VALUE, seq, &r, sizeof r);
        return;
    }
    case MSG_SET_LIMITS: {
        DECODE(msg_set_limits_t, m);
        if (!axis_ok(m.axis) || (m.enabled && m.max <= m.min)) {
            ack(id, seq, m.axis < MAX_AXES ? ACK_BAD_ARG : ACK_NOT_CONFIGURED);
            return;
        }
        c.type = CMD_SET_LIMITS;
        c.axis = m.axis;
        c.i1 = m.min;
        c.i2 = m.max;
        c.mask = m.enabled ? 1 : 0;
        ack(id, seq, push(&c));
        return;
    }
    case MSG_ENABLE: {
        DECODE(msg_enable_t, m);
        c.type = CMD_ENABLE;
        c.mask = m.axis_mask;
        c.mask2 = m.enable_mask;
        ack(id, seq, push(&c));
        return;
    }
    case MSG_SET_VELOCITY: {               /* streamed, no ACK */
        DECODE(msg_set_velocity_t, m);
        c.type = CMD_SET_VELOCITY;
        c.mask = m.axis_mask;
        memcpy(c.f, m.v, sizeof c.f);
        push(&c);
        return;
    }
    case MSG_MOVE_TO: {
        DECODE(msg_move_to_t, m);
        if (!axis_ok(m.axis)) {
            ack(id, seq, ACK_NOT_CONFIGURED);
            return;
        }
        if (estop) {
            ack(id, seq, ACK_ESTOPPED);
            return;
        }
        c.type = CMD_MOVE_TO;
        c.axis = m.axis;
        c.i1 = m.target;
        c.f[0] = m.max_vel;
        c.f[1] = m.accel;
        ack(id, seq, push(&c));
        return;
    }
    case MSG_STOP: {
        DECODE(msg_stop_t, m);
        c.type = CMD_STOP;
        c.mask = m.axis_mask;
        ack(id, seq, push(&c));
        return;
    }
    case MSG_ESTOP:
        estop = true;
        c.type = CMD_ESTOP;
        ack(id, seq, push(&c));
        return;
    case MSG_CLEAR_ESTOP:
        estop = false;
        c.type = CMD_CLEAR_ESTOP;
        ack(id, seq, push(&c));
        return;
    case MSG_HOME: {
        DECODE(msg_home_t, m);
        if (!axis_ok(m.axis) || !has_endstop[m.axis]) {
            ack(id, seq, ACK_NOT_CONFIGURED);
            return;
        }
        if (estop) {
            ack(id, seq, ACK_ESTOPPED);
            return;
        }
        if (m.velocity == 0.0f || m.max_travel <= 0) {
            ack(id, seq, ACK_BAD_ARG);
            return;
        }
        c.type = CMD_HOME;
        c.axis = m.axis;
        c.f[0] = m.velocity;
        c.i1 = m.max_travel;
        ack(id, seq, push(&c));
        return;
    }
    case MSG_SET_POSITION: {
        DECODE(msg_set_position_t, m);
        if (!axis_ok(m.axis)) {
            ack(id, seq, ACK_NOT_CONFIGURED);
            return;
        }
        c.type = CMD_SET_POSITION;
        c.axis = m.axis;
        c.i1 = m.position;
        ack(id, seq, push(&c));
        return;
    }
    case MSG_KEEPALIVE:
        c.type = CMD_FEED_WATCHDOG;
        push(&c);
        return;
    default:
        ack(id, seq, ACK_UNKNOWN_CMD);
        return;
    }
}
