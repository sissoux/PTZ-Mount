# PTZ firmware (RP2040 / SKR Pico)

C firmware built with the Raspberry Pi Pico SDK. It executes motion in real
time and knows nothing about the mechanics: the host sends pins, limits, speeds
and driver registers at startup.

| File | Role |
|---|---|
| `src/main.c` | Startup, core 0 main loop (link, events, status, watchdog) |
| `src/comm.c` | UART0 host link, interrupt RX ring buffer, COBS framing |
| `src/commands.c` | Message decoding and validation, ACKs, queueing to core 1 |
| `src/stepper.c` | **Core 1**: 40 kHz step ISR + 1 kHz control loop |
| `src/tmc_uart.c` | Raw TMC2209 register read/write on UART1 (GPIO8/9) |
| `src/cobs.c` | COBS + CRC16 |
| `src/protocol.h` | Message ids and payload structs (mirror of `host/ptz/protocol.py`) |
| `src/config.h` | Compile-time settings (host UART pins/baud, tick rates) |

## Build

```sh
cmake -B build -DPICO_SDK_FETCH_FROM_GIT=ON     # or export PICO_SDK_PATH=...
cmake --build build -j4
# -> build/ptz_firmware.uf2
```

Flashing is described in [docs/raspberry-pi-setup.md](../docs/raspberry-pi-setup.md).

## Debugging

`printf` goes to USB CDC (the USB-C port), never to UART0, which is the host
link. `comm_log()` sends a `LOG` message that appears in the host log.

## Rules when changing the firmware

* Any protocol change goes into `protocol.h`, `host/ptz/protocol.py`,
  `docs/protocol.md`, and bumps `PROTOCOL_VERSION`.
* Any change to the control law in `stepper.c` must be mirrored in
  `host/ptz/sim.py`, so the simulator and tests stay meaningful.
* The step ISR runs from RAM and uses integers only. Keep it that way.
