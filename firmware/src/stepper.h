/*
 * Real-time motion engine, running entirely on core 1.
 *
 *  - step ISR at STEP_TICK_HZ: DDS phase accumulator per axis, step pulses,
 *    direction changes, endstop guard (halts on the very tick it triggers)
 *  - control loop at CONTROL_HZ: velocity ramps, point-to-point moves,
 *    homing, soft limits, command watchdog
 *
 * Core 0 talks to it only through two lock-free queues (commands in,
 * events out) and a status snapshot protected by a critical section.
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "protocol.h"

typedef enum {
    CMD_RESET,
    CMD_CONFIG_AXIS,
    CMD_ENABLE,
    CMD_SET_VELOCITY,
    CMD_MOVE_TO,
    CMD_STOP,
    CMD_ESTOP,
    CMD_CLEAR_ESTOP,
    CMD_HOME,
    CMD_SET_POSITION,
    CMD_SET_LIMITS,
    CMD_SET_WATCHDOG,
    CMD_FEED_WATCHDOG,
} stepper_cmd_type_t;

typedef struct {
    uint8_t type;
    uint8_t axis;
    uint8_t mask;
    uint8_t mask2;
    int32_t i1, i2;
    float   f[MAX_AXES];
    msg_config_axis_t cfg;
} stepper_cmd_t;

/* core 0 side */
void     stepper_init(void);                         /* before launching core 1 */
bool     stepper_push(const stepper_cmd_t *cmd);     /* false if queue full */
bool     stepper_pop_event(msg_event_t *ev);
void     stepper_get_status(msg_status_t *st);
uint32_t stepper_heartbeat(void);

/* core 1 entry point */
void stepper_core1_main(void);
