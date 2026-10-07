/*
 * Host <-> MCU binary protocol. MUST stay in sync with host/ptz/protocol.py.
 * See docs/protocol.md for the full description.
 */
#pragma once
#include <stdint.h>

#define PROTOCOL_VERSION 1
#define MAX_AXES         4
#define PIN_NONE         0xFF
#define MAX_RAW          64

#define PACKED __attribute__((packed))

/* ------------------------------------------------------------ message ids */
enum {
    /* host -> mcu */
    MSG_PING            = 0x01,
    MSG_RESET           = 0x02,
    MSG_SET_STATUS_RATE = 0x03,
    MSG_SET_WATCHDOG    = 0x04,
    MSG_CONFIG_AXIS     = 0x10,
    MSG_CONFIG_TMC_UART = 0x11,
    MSG_TMC_WRITE       = 0x12,
    MSG_TMC_READ        = 0x13,
    MSG_SET_LIMITS      = 0x14,
    MSG_ENABLE          = 0x20,
    MSG_SET_VELOCITY    = 0x21,
    MSG_MOVE_TO         = 0x22,
    MSG_STOP            = 0x23,
    MSG_ESTOP           = 0x24,
    MSG_HOME            = 0x25,
    MSG_SET_POSITION    = 0x26,
    MSG_KEEPALIVE       = 0x27,
    MSG_CLEAR_ESTOP     = 0x28,
    /* mcu -> host */
    MSG_ACK             = 0x80,
    MSG_PONG            = 0x81,
    MSG_STATUS          = 0x82,
    MSG_EVENT           = 0x83,
    MSG_TMC_VALUE       = 0x84,
    MSG_LOG             = 0x85,
};

/* ------------------------------------------------------------ constants */
enum {
    ACK_OK = 0, ACK_UNKNOWN_CMD, ACK_BAD_LENGTH, ACK_BAD_ARG,
    ACK_NOT_CONFIGURED, ACK_ESTOPPED, ACK_QUEUE_FULL, ACK_TMC_ERROR,
};

/* CONFIG_AXIS.flags */
#define AXF_DIR_INVERT      (1u << 0)
#define AXF_ENABLE_INVERT   (1u << 1)
#define AXF_ENDSTOP_INVERT  (1u << 2)
#define AXF_ENDSTOP_PULLUP  (1u << 3)

/* STATUS.sys_flags */
#define SYS_ESTOP      (1u << 0)
#define SYS_WATCHDOG   (1u << 1)
#define SYS_TMC_READY  (1u << 2)

/* STATUS axis flags */
#define ST_ENABLED       (1u << 0)
#define ST_MOVING        (1u << 1)
#define ST_ENDSTOP       (1u << 2)
#define ST_HOMING        (1u << 3)
#define ST_AT_LIMIT      (1u << 4)
#define ST_CONFIGURED    (1u << 5)
#define ST_HALTED        (1u << 6)
#define ST_POSITION_MODE (1u << 7)

/* EVENT types */
enum {
    EV_MOVE_DONE = 1, EV_HOME_TRIGGERED, EV_HOME_FAILED, EV_LIMIT_HIT,
    EV_ENDSTOP_HIT, EV_WATCHDOG, EV_BOOT,
};

/* ------------------------------------------------------------ payloads */
typedef struct PACKED { uint16_t rate_hz; } msg_set_status_rate_t;
typedef struct PACKED { uint16_t timeout_ms; } msg_set_watchdog_t;

typedef struct PACKED {
    uint8_t axis, step_pin, dir_pin, enable_pin, endstop_pin, flags;
    int8_t  endstop_dir;
    float   max_vel, max_accel, vel_accel;
} msg_config_axis_t;

typedef struct PACKED { uint8_t rx_pin, tx_pin; uint32_t baud; } msg_config_tmc_uart_t;
typedef struct PACKED { uint8_t addr, reg; uint32_t value; } msg_tmc_write_t;
typedef struct PACKED { uint8_t addr, reg; } msg_tmc_read_t;
typedef struct PACKED { uint8_t axis; int32_t min, max; int8_t enabled; } msg_set_limits_t;
typedef struct PACKED { uint8_t axis_mask, enable_mask; } msg_enable_t;
typedef struct PACKED { uint8_t axis_mask; float v[MAX_AXES]; } msg_set_velocity_t;
typedef struct PACKED { uint8_t axis; int32_t target; float max_vel, accel; } msg_move_to_t;
typedef struct PACKED { uint8_t axis_mask; } msg_stop_t;
typedef struct PACKED { uint8_t axis; float velocity; int32_t max_travel; } msg_home_t;
typedef struct PACKED { uint8_t axis; int32_t position; } msg_set_position_t;

typedef struct PACKED { uint8_t acked_id, status; } msg_ack_t;
typedef struct PACKED { uint16_t protocol_version, max_axes; uint32_t uptime_ms; } msg_pong_t;
typedef struct PACKED { int32_t pos; float vel; uint8_t flags; } msg_axis_status_t;
typedef struct PACKED {
    uint32_t time_ms;
    uint8_t  sys_flags;
    msg_axis_status_t axes[MAX_AXES];
} msg_status_t;
typedef struct PACKED { uint8_t type, axis; int32_t value; } msg_event_t;
typedef struct PACKED { uint8_t addr, reg; uint32_t value; uint8_t ok; } msg_tmc_value_t;

/* Sizes are part of the protocol: keep them checked */
_Static_assert(sizeof(msg_config_axis_t) == 19, "CONFIG_AXIS size");
_Static_assert(sizeof(msg_set_velocity_t) == 17, "SET_VELOCITY size");
_Static_assert(sizeof(msg_status_t) == 41, "STATUS size");
_Static_assert(sizeof(msg_status_t) + 4 <= MAX_RAW, "STATUS too large");
