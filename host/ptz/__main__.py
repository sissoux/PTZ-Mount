"""PTZ daemon entry point.

    python -m ptz -c ../config/ptz.cfg            # real hardware
    python -m ptz -c ../config/ptz.cfg --sim      # simulated MCU (no hardware)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

from . import __version__
from .config import ConfigError, load
from .mcu import McuLink, SerialTransport
from .motion import MotionController

log = logging.getLogger("ptz")


async def run(args) -> None:
    cfg = load(args.config)
    for w in cfg.warnings:
        log.warning("config: %s", w)
    if args.state_dir:
        cfg.server.state_dir = args.state_dir

    if args.sim:
        from .sim import SimTransport
        transport = SimTransport()
        log.info("using SIMULATED MCU")
    else:
        transport = SerialTransport(args.serial or cfg.mcu.serial, cfg.mcu.baud)

    ctrl = MotionController(cfg, McuLink(transport))
    await ctrl.start()

    from .api.http import HttpServer
    servers = [HttpServer(ctrl, cfg.server.http_host, args.port or cfg.server.http_port)]
    if cfg.server.udp_port:
        from .api.udp import UdpServer
        servers.append(UdpServer(ctrl, cfg.server.http_host, cfg.server.udp_port))
    if cfg.server.visca_port:
        from .api.visca import ViscaServer
        servers.append(ViscaServer(ctrl, cfg.server.http_host, cfg.server.visca_port))
    for s in servers:
        await s.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl+C raises KeyboardInterrupt instead

    period = 1.0 / cfg.server.status_rate
    try:
        while not stop.is_set():
            status = {"type": "status", **ctrl.status()}
            for s in servers:
                if hasattr(s, "broadcast"):
                    await s.broadcast(status)
            try:
                await asyncio.wait_for(stop.wait(), period)
            except asyncio.TimeoutError:
                pass
    finally:
        for s in servers:
            await s.close()
        await ctrl.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="ptz", description="PTZ camera head daemon")
    ap.add_argument("-c", "--config", default=os.environ.get("PTZ_CONFIG", "config/ptz.cfg"))
    ap.add_argument("--sim", action="store_true", help="use the simulated MCU")
    ap.add_argument("--serial", help="override [mcu] serial")
    ap.add_argument("--port", type=int, help="override [server] http_port")
    ap.add_argument("--state-dir", help="override [server] state_dir")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        asyncio.run(run(args))
    except ConfigError as e:
        log.error("configuration error: %s", e)
        raise SystemExit(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
