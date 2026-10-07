# Raspberry Pi and SKR Pico setup

## 1. Stop Klipper

The PTZ daemon needs exclusive use of the serial port.

```sh
sudo systemctl disable --now klipper moonraker 2>/dev/null
```

Klipper stays installed and can be re-enabled later. Its MCU firmware is
replaced by the PTZ firmware, though (see step 3).

## 2. Enable the PL011 UART on the GPIO header

The Pi 4 has two UARTs. The good one (PL011) is used by Bluetooth by default.
Edit `/boot/firmware/config.txt` (Bookworm) or `/boot/config.txt` (older):

```ini
enable_uart=1
dtoverlay=disable-bt
```

Then disable the serial login console and Bluetooth modem service. The
interactive equivalent is `sudo raspi-config`, then Interface Options, Serial
Port: login shell **No**, serial hardware **Yes**.

```sh
sudo raspi-config nonint do_serial_cons 1      # no login shell on serial
sudo raspi-config nonint do_serial_hw 0        # keep the serial hardware enabled
sudo systemctl disable hciuart
sudo usermod -aG dialout $USER
sudo reboot
```

After the reboot, `/dev/serial0` must point to `ttyAMA0`:

```sh
ls -l /dev/serial0
```

If Klipper was already working over UART, all of this is probably done already.

Wiring (SKR Pico "Raspberry Pi" header): Pi TX (GPIO14) goes to SKR RX (GPIO1),
Pi RX (GPIO15) goes to SKR TX (GPIO0), and GND to GND.

## 3. Build and flash the firmware

On the Pi (or any Linux/WSL machine):

```sh
sudo apt install cmake gcc-arm-none-eabi libnewlib-arm-none-eabi build-essential git
cd firmware
cmake -B build -DPICO_SDK_FETCH_FROM_GIT=ON
cmake --build build -j4
```

This produces `firmware/build/ptz_firmware.uf2`.

To flash it, connect the SKR Pico's USB-C port to the Pi or a PC. Hold the
**BOOT** button, press and release **RESET**, then release BOOT. A drive named
`RPI-RP2` appears. Copy the `.uf2` file onto it.

```sh
# from the Pi, if the drive is auto-mounted:
cp build/ptz_firmware.uf2 /media/$USER/RPI-RP2/
# or with picotool:
sudo picotool load -x build/ptz_firmware.uf2
```

If the firmware link baud rate is changed (`-DHOST_UART_BAUD=...`), set the same
value in `[mcu] baud` in `ptz.cfg`.

## 4. Install the daemon

```sh
git clone <this repo> ~/ptz && cd ~/ptz
./deploy/install.sh
```

Open `http://<pi-address>:8080/`.

Logs:

```sh
journalctl -u ptz -f
```
