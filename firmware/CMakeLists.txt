cmake_minimum_required(VERSION 3.13)

# Pico SDK: set PICO_SDK_PATH, or configure with -DPICO_SDK_FETCH_FROM_GIT=ON
include(pico_sdk_import.cmake)

project(ptz_firmware C CXX ASM)
set(CMAKE_C_STANDARD 11)

pico_sdk_init()

add_executable(ptz_firmware
    src/main.c
    src/comm.c
    src/commands.c
    src/stepper.c
    src/tmc_uart.c
    src/cobs.c
)

target_compile_options(ptz_firmware PRIVATE -Wall -Wextra -O2)

# Optional: cmake -DHOST_UART_BAUD=1000000 ..
if (DEFINED HOST_UART_BAUD)
    target_compile_definitions(ptz_firmware PRIVATE HOST_UART_BAUD=${HOST_UART_BAUD})
endif()

target_link_libraries(ptz_firmware
    pico_stdlib
    pico_multicore
    hardware_uart
    hardware_gpio
    hardware_sync
    hardware_watchdog
)

# UART0 is the Raspberry Pi link: keep stdio on USB only
pico_enable_stdio_usb(ptz_firmware 1)
pico_enable_stdio_uart(ptz_firmware 0)

pico_add_extra_outputs(ptz_firmware)   # produces ptz_firmware.uf2
