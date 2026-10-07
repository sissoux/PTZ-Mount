# Architecture

## Goals

* Live, low-latency joystick control of a DSLR PTZ head (pan, tilt, zoom,
  optional focus), from a web page, a dedicated application or a network
  hardware joystick.
* Klipper-like text configuration: pins, gearing, limits, speeds and driver
  currents are edited in `config/ptz.cfg`, no recompilation.
* Python everywhere except the hard real-time part (step generation), which
  runs in C on the RP2040.

## Why not Klipper

Klipper is built for 3D printing. The host plans moves ahead of time and
schedules them on the MCU in the future (a buffer of roughly 100 ms or more).
That gives perfect trajectories for G-code, but every change of direction or
speed from a joystick has to wait for the queue to drain. That is the latency
that was observed.

A PTZ head needs the opposite: the **current** velocity target must reach the
motors within a few milliseconds, and the MCU must ramp smoothly toward
whatever it is told last. So the planning moves into the MCU, in
"velocity mode". The host only says *how fast* (jog) or *where to* (goto).

## Overview

```
 Browser (web UI)   Companion / OBS / VISCA keyboard    Gamepad bridge / custom app
        |  HTTP + WebSocket          |  VISCA over IP (UDP 52381)     |  JSON over UDP (9000)
        v                            v                                v
 +-----------------------------------------------------------------------------+
 | Raspberry Pi 4  -  Python 3, asyncio  (host/ptz)                             |
 |                                                                              |
 |  api/http.py   api/visca.py   api/udp.py     -> api/commands.py (dispatcher) |
 |                                    |                                         |
 |                         motion.py  MotionController                          |
 |   units <-> microsteps, jog shaping (deadband/expo), synchronized goto,      |
 |   homing sequence, presets, enable/estop, TMC2209 register values            |
 |                                    |                                         |
 |                  mcu.py  McuLink (request/ACK, status, events)               |
 |                  protocol.py  COBS + CRC16 binary frames                     |
 +-----------------------------------|-----------------------------------------+
                                     |  UART 500 kbaud (/dev/serial0 <-> GPIO0/1)
 +-----------------------------------v-----------------------------------------+
 | RP2040 on SKR Pico  -  C, Pico SDK  (firmware/)                              |
 |                                                                              |
 |  core 0: comm.c (UART IRQ ring buffer), commands.c (validate, queue),        |
 |          tmc_uart.c (raw TMC2209 register access), status/events             |
 |                   | lock-free queue          ^ event queue + status snapshot |
 |  core 1: stepper.c                                                           |
 |          - step ISR 40 kHz: DDS phase accumulators, step/dir pulses,         |
 |            endstop guard (halts on the exact tick the switch triggers)       |
 |          - control loop 1 kHz: velocity ramps, position moves, homing,       |
 |            soft limits, command watchdog                                     |
 +------------------------------------------------------------------------------+
        |  STEP/DIR/EN x4                       | single-wire UART (GPIO8/9)
        v                                       v
   TMC2209 X (pan)  Y (tilt)  Z (zoom)  E (focus, optional)      endstops GPIO4/3/25
```

## Split of responsibilities

| Concern | Where | Why |
|---|---|---|
| Step pulses, direction, endstop halt | RP2040 ISR | microsecond timing |
| Acceleration ramps, braking before soft limits | RP2040 1 kHz loop | must never depend on the network or Linux scheduling |
| Command watchdog (stop if the host goes silent) | RP2040 | survives a host crash |
| Units, gearing, config parsing | Python | easy to edit |
| Jog curve (deadband, expo, speed factor) | Python | tuning without reflashing |
| Homing *sequence* (approach, retract, slow approach) | Python | only the "stop at the switch" part is real-time |
| Multi-axis synchronization of goto moves | Python | scales each axis profile so all arrive together |
| TMC2209 register values (current, microsteps, stealthChop) | Python | the MCU only forwards raw registers |
| Presets, network protocols, UI | Python | |

The firmware is **board-agnostic**: every pin arrives from the host at startup
(`CONFIG_AXIS`). Only the host UART pins are compiled in (`firmware/src/config.h`).

## Latency budget (joystick to motor)

| Step | Typical |
|---|---|
| Client sends a jog (WebSocket or UDP on the LAN) | 1 to 5 ms |
| Python parses, shapes, encodes (one frame of about 25 bytes) | < 1 ms |
| UART at 500 kbaud | 0.5 ms |
| Core 0 to core 1 queue, picked up by the 1 kHz loop | < 1 ms |
| Ramp toward the new target | immediate, limited by `jog_accel` |

The total is well under 10 ms, compared with more than 100 ms for queued Klipper moves.

## Safety layers

1. **Jog refresh**: every jog source must repeat its command at least every
   `motion.jog_timeout`. A stale source is zeroed by the host.
2. **MCU watchdog**: if no `SET_VELOCITY`/`KEEPALIVE` arrives within
   `mcu.watchdog_timeout`, every jogging axis decelerates to 0. This covers a
   crashed host or a broken UART.
3. **Hardware watchdog**: the RP2040 reboots if core 1 stops running. The host
   sees the `BOOT` event, reconfigures and marks the axes unhomed.
4. **Soft limits** are enforced on the MCU once an axis is homed. The axis
   brakes so that it stops at the limit.
5. **Endstop guard**: outside homing, an axis moving toward its endstop is
   halted on the very ISR tick the switch closes.
6. **E-stop** halts step generation instantly but **keeps the drivers
   powered**, so the tilt axis keeps holding the camera. Releasing the motors is
   a separate, explicit action.

## Simulator

`host/ptz/sim.py` speaks the same binary protocol and implements the same
control law as `firmware/src/stepper.c`. `python -m ptz --sim` runs the full
stack on a PC, and the automated tests use it. When changing the firmware
behaviour, update the simulator too.

## Extension ideas

* **Focus axis** on the 4th driver (E0), already supported: uncomment
  `[axis focus]`.
* **Camera control** with gphoto2 (record, ISO, aperture) as another Python
  module and API commands.
* **Sensorless homing** with the TMC2209 StallGuard DIAG output (SKR Pico
  jumpers) to reach a mechanical stop without switches.
* **PIO-based step generation**, if step rates above 20 kHz per axis are needed.
* **S-curve (jerk-limited) ramps** in `control_axis()` for even smoother
  starts and stops on long lenses.
* **Timed camera moves**: keyframed trajectories (time-lapse, motion
  control) streamed as `MOVE_TO` or velocity setpoints from Python.
* **Zoom/focus lens calibration tables**: map the ring angle to focal length.
