"""HTTP server: web UI, REST API and WebSocket live control."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Set

from aiohttp import WSMsgType, web

from .. import weblog
from ..motion import MotionController
from .commands import config_summary, dispatch

log = logging.getLogger("ptz.http")
WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


class HttpServer:
    def __init__(self, ctrl: MotionController, host: str, port: int):
        self.ctrl, self.host, self.port = ctrl, host, port
        self.clients: Set[web.WebSocketResponse] = set()
        self.log_clients: Set[web.WebSocketResponse] = set()
        self.logs = weblog.install()
        self.app = web.Application()
        self.app.add_routes([
            web.get("/", self._index),
            web.get("/ws", self._ws),
            web.get("/api/status", self._status),
            web.get("/api/config", self._config),
            web.get("/api/presets", self._presets),
            web.post("/api/cmd", self._cmd),
            web.static("/static", WEB_ROOT),
        ])
        self._runner = None

    async def start(self) -> None:
        self.logs.loop = asyncio.get_running_loop()
        self.logs.listeners.append(self._on_log)
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        log.info("web UI on http://%s:%d/", self.host, self.port)

    async def close(self) -> None:
        for ws in list(self.clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    def _on_log(self, entry: dict) -> None:
        if not self.log_clients:
            return
        data = json.dumps({"type": "log", **entry})
        for ws in list(self.log_clients):
            if ws.closed:
                self.log_clients.discard(ws)
            else:
                asyncio.get_running_loop().create_task(self._send_quiet(ws, data))

    @staticmethod
    async def _send_quiet(ws, data: str) -> None:
        try:
            await ws.send_str(data)
        except Exception:  # noqa: BLE001
            pass

    async def broadcast(self, payload: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(payload)
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except Exception:  # noqa: BLE001
                self.clients.discard(ws)

    # ------------------------------------------------------------ handlers
    async def _index(self, request):
        return web.FileResponse(os.path.join(WEB_ROOT, "index.html"))

    async def _status(self, request):
        return web.json_response(self.ctrl.status())

    async def _config(self, request):
        return web.json_response(config_summary(self.ctrl))

    async def _presets(self, request):
        return web.json_response(self.ctrl.presets.all())

    async def _cmd(self, request):
        try:
            msg = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        reply = await dispatch(self.ctrl, msg)
        return web.json_response(reply, status=200 if reply["ok"] else 400)

    async def _ws(self, request):
        ws = web.WebSocketResponse(heartbeat=5.0)
        await ws.prepare(request)
        self.clients.add(ws)
        jogging = set()
        try:
            await ws.send_str(json.dumps({"type": "config", **config_summary(self.ctrl)}))
            await ws.send_str(json.dumps({"type": "presets",
                                          "presets": self.ctrl.presets.all()}))
            await ws.send_str(json.dumps({"type": "recordings",
                                          "recordings": self.ctrl.recorder.list()}))
            await ws.send_str(json.dumps({"type": "debug", "on": weblog.is_debug()}))
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(m.data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("cmd") == "logs":            # debug console subscription
                    if msg.get("on", True):
                        self.log_clients.add(ws)
                        await ws.send_str(json.dumps({"type": "log_backlog",
                                                      "entries": list(self.logs.buffer)}))
                    else:
                        self.log_clients.discard(ws)
                    continue
                if msg.get("cmd") == "jog":
                    jogging.update(k for k in msg if k in self.ctrl.state)
                reply = await dispatch(self.ctrl, msg, source=request.remote or "web")
                if "id" in msg or not reply["ok"]:
                    reply.update(type="reply", id=msg.get("id"), cmd=msg.get("cmd"))
                    await ws.send_str(json.dumps(reply))
                if "presets" in reply:
                    await self.broadcast({"type": "presets", "presets": reply["presets"]})
                if "recordings" in reply:
                    await self.broadcast({"type": "recordings",
                                          "recordings": reply["recordings"]})
                if msg.get("cmd") == "debug":
                    await self.broadcast({"type": "debug", "on": weblog.is_debug()})
        finally:
            self.clients.discard(ws)
            self.log_clients.discard(ws)
            if jogging:   # client vanished while jogging: stop right now
                self.ctrl.jog({k: 0.0 for k in jogging})
        return ws
