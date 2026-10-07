# Host <-> MCU protocol

Binary, little-endian, framed protocol over the UART between the Raspberry Pi
(`/dev/serial0`, PL011) and the RP2040 (UART0, GPIO0 = TX, GPIO1 = RX).

Implementations that MUST stay in sync:

* `firmware/src/protocol.h`
* `host/ptz/protocol.py`
* `host/ptz/sim.py` (Python model of the firmware behaviour)

Bump `PROTOCOL_VERSION` in all of them when anything below changes.

## Framing

```
raw    = msg_id:u8 | seq:u8 | payload:N bytes | crc16:u16 (LE)
frame  = COBS(raw) | 0x00
```

* CRC16-CCITT (poly 0x1021, init 0xFFFF), computed over `msg_id | seq | payload`.
* COBS removes every 0x00 from the frame, so 0x00 is the frame delimiter.
  A receiver that loses sync simply waits for the next 0x00.
* `seq` is chosen by the host. Replies (`ACK`, `PONG`, `TMC_VALUE`) echo the
  `seq` of the request. Unsolicited MCU messages (`STATUS`, `EVENT`, `LOG`)
  use `seq = 0`.
* Max raw size: 64 bytes.

## Units

* Positions: **microsteps**, `int32`.
* Velocities: **microsteps / s**, `float32` (signed).
* Accelerations: **microsteps / s^2**, `float32`.
* Pins: RP2040 GPIO number, `0xFF` = not used.

All conversion to user units (degrees, %, mm...) happens on the host.

## Host -> MCU

| id   | name             | payload (struct format)              | reply      |
|------|------------------|--------------------------------------|------------|
| 0x01 | PING             | -                                    | PONG       |
| 0x02 | RESET            | -                                    | ACK        |
| 0x03 | SET_STATUS_RATE  | `H` rate_hz                          | ACK        |
| 0x04 | SET_WATCHDOG     | `H` timeout_ms (0 = off)             | ACK        |
| 0x10 | CONFIG_AXIS      | `BBBBBBbfff` axis, step_pin, dir_pin, enable_pin, endstop_pin, flags, endstop_dir, max_vel, max_accel, vel_accel | ACK |
| 0x11 | CONFIG_TMC_UART  | `BBI` rx_pin, tx_pin, baud           | ACK        |
| 0x12 | TMC_WRITE        | `BBI` addr, reg, value               | ACK        |
| 0x13 | TMC_READ         | `BB` addr, reg                       | TMC_VALUE  |
| 0x14 | SET_LIMITS       | `Biib` axis, min, max, enabled       | ACK        |
| 0x20 | ENABLE           | `BB` axis_mask, enable_mask          | ACK        |
| 0x21 | SET_VELOCITY     | `B4f` axis_mask, v0, v1, v2, v3      | none (stream) |
| 0x22 | MOVE_TO          | `Biff` axis, target, max_vel, accel  | ACK, later EVENT MOVE_DONE |
| 0x23 | STOP             | `B` axis_mask (controlled decel)     | ACK        |
| 0x24 | ESTOP            | - (instant halt, drivers stay enabled) | ACK      |
| 0x25 | HOME             | `Bfi` axis, velocity, max_travel     | ACK, later EVENT HOME_TRIGGERED / HOME_FAILED |
| 0x26 | SET_POSITION     | `Bi` axis, position                  | ACK        |
| 0x27 | KEEPALIVE        | -                                    | none       |
| 0x28 | CLEAR_ESTOP      | -                                    | ACK        |

`CONFIG_AXIS.flags`: bit0 dir_invert, bit1 enable_invert, bit2 endstop_invert,
bit3 endstop_pullup.

`CONFIG_AXIS.vel_accel`: acceleration used in velocity (jog) mode. `max_accel`
is the hard limit used by `MOVE_TO` (clamped), `STOP` and the watchdog stop.

`CONFIG_AXIS.endstop_dir`: -1 endstop at the negative end, +1 at the positive
end, 0 no endstop guard. Outside of homing the MCU halts the axis instantly if
the endstop is hit while moving toward it.

`SET_VELOCITY` puts the masked axes in velocity mode (it overrides a running
`MOVE_TO`). It also feeds the watchdog. If no `SET_VELOCITY`/`KEEPALIVE` is
received within the watchdog timeout, all axes in velocity mode decelerate to 0.

`HOME` moves the axis at `velocity` (sign = direction) until its endstop
triggers. The step ISR halts the axis on the very tick the endstop is seen.
The multi-phase homing sequence (fast approach, retract, slow approach, set
position) is orchestrated by the host.

`ESTOP` stops step generation immediately but keeps the drivers enabled so
that the tilt axis keeps holding the camera. Use `ENABLE` to release motors.
`CLEAR_ESTOP` is required before any new motion.

## MCU -> Host

| id   | name      | payload                                          |
|------|-----------|--------------------------------------------------|
| 0x80 | ACK       | `BB` acked_msg_id, status                        |
| 0x81 | PONG      | `HHI` protocol_version, max_axes, uptime_ms      |
| 0x82 | STATUS    | `IB` time_ms, sys_flags + 4 x `ifB` pos, vel, axis_flags |
| 0x83 | EVENT     | `BBi` type, axis, value                          |
| 0x84 | TMC_VALUE | `BBIB` addr, reg, value, ok                      |
| 0x85 | LOG       | UTF-8 text (variable length)                     |

ACK status: 0 OK, 1 UNKNOWN_CMD, 2 BAD_LENGTH, 3 BAD_ARG, 4 NOT_CONFIGURED,
5 ESTOPPED, 6 QUEUE_FULL, 7 TMC_ERROR.

`sys_flags`: bit0 estop, bit1 watchdog_expired, bit2 tmc_uart_ready.

`axis_flags`: bit0 enabled, bit1 moving, bit2 endstop_active, bit3 homing,
bit4 at_soft_limit, bit5 configured, bit6 halted_by_endstop,
bit7 position_mode.

EVENT types:

| type | name            | value                    |
|------|-----------------|--------------------------|
| 1    | MOVE_DONE       | final position           |
| 2    | HOME_TRIGGERED  | position at trigger      |
| 3    | HOME_FAILED     | position when aborted    |
| 4    | LIMIT_HIT       | position                 |
| 5    | ENDSTOP_HIT     | position (outside homing)|
| 6    | WATCHDOG        | 0                        |
| 7    | BOOT            | 1 if reset by the hardware watchdog, else 0 |
