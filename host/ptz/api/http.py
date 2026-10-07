"""HTTP server: web UI, REST API, WebSocket live control, recording files."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Dict

from aiohttp import WSMsgType, web

from .. import weblog
from ..motion import LOCAL_ADDRESSES, MotionController
from ..recorder import RecorderError
from .commands import config_summary, dispatch

log = logging.getLogger("ptz.http")
WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
MAX_UPLOAD = 20 * 1024 * 1024


def _agent(ua: str) -> str:
    """Very small User-Agent summary: 'Chrome / Windows'."""
    ua = ua or ""
    browser = next((b for b, key in (("Edge", "Edg/"), ("Firefox", "Firefox/"),
                                     ("Chrome", "Chrome/"), ("Safari", "Safari/"))
                    if key in ua), "")
    system = next((o for o, key in (("iPhone", "iPhone"), ("iPad", "iPad"),
                                    ("Android", "Android"), ("Windows", "Windows"),
                                    ("macOS", "Mac OS"), ("Linux", "Linux"))
                   if key in ua), "")
    return " / ".join(x for x in (browser, system) if x) or "client"


class WebClient:
    def __init__(self, ws: web.WebSocketResponse, request: web.Request):
        self.ws = ws
        self.id = uuid.uuid4().hex[:8]
        self.ip = request.remote or "?"
        self.agent = _agent(request.headers.get("User-Agent", ""))
        self.since = time.time()

    @property
    def label(self) -> str:
        return f"{self.ip} ({self.agent})"


class HttpServer:
    def __init__(self, ctrl: MotionController, host: str, port: int):
        self.ctrl, self.host, self.port = ctrl, host, port
        self.clients: Dict[web.WebSocketResponse, WebClient] = {}
        self.log_clients = set()
        self.logs = weblog.install()
        self.app = web.Application(client_max_size=MAX_UPLOAD)
        self.app.add_routes([
            web.get("/", self._index),
            web.get("/ws", self._ws),
            web.get("/api/status", self._status),
            web.get("/api/config", self._config),
            web.get("/api/presets", self._presets),
            web.post("/api/cmd", self._cmd),
            web.get("/api/recordings", self._recordings),
            web.get("/api/recordings/{name}", self._download),
            web.post("/api/recordings", self._upload),
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

    # ------------------------------------------------------------ push helpers
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
                self.clients.pop(ws, None)

    async def broadcast_clients(self) -> None:
        """Who is connected (sent to each client with its own id)."""
        owner = self.ctrl.lock_owner
        lst = [{"id": c.id, "ip": c.ip, "agent": c.agent, "since": c.since,
                "owner": c.id == owner} for c in self.clients.values()]
        for ws, c in list(self.clients.items()):
            await self._send_quiet(ws, json.dumps({"type": "clients", "you": c.id,
                                                   "clients": lst}))

    # ------------------------------------------------------------ REST
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
        reply = await dispatch(self.ctrl, msg, source=f"http {request.remote}",
                               client=request.headers.get("X-PTZ-Client"),
                               local=request.remote in LOCAL_ADDRESSES)
        if isinstance(msg, dict) and msg.get("cmd") in ("lock", "unlock"):
            await self.broadcast_clients()
        return web.json_response(reply, status=200 if reply["ok"] else 400)

    async def _recordings(self, request):
        return web.json_response(self.ctrl.recorder.list())

    async def _download(self, request):
        name = request.match_info["name"]
        try:
            data = self.ctrl.recorder.load(name)
        except RecorderError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=404)
        fname = re.sub(r'[^A-Za-z0-9 _.()-]', "_", data["name"]) + ".json"
        return web.Response(text=json.dumps(data, indent=1), content_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    async def _upload(self, request):
        client = request.headers.get("X-PTZ-Client")
        if not self.ctrl.may_control(client):
            return web.json_response({"ok": False, "error":
                                      f"control is locked by {self.ctrl.lock_label}"}, status=403)
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "file is not valid JSON"}, status=400)
        try:
            summary = self.ctrl.import_recording(data, request.query.get("name", ""))
        except RecorderError as e:
            log.warning("upload rejected: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        recs = self.ctrl.recorder.list()
        await self.broadcast({"type": "recordings", "recordings": recs})
        return web.json_response({"ok": True, "recording": summary})

    # ------------------------------------------------------------ WebSocket
    async def _ws(self, request):
        ws = web.WebSocketResponse(heartbeat=5.0)
        await ws.prepare(request)
        me = WebClient(ws, request)
        self.clients[ws] = me
        log.info("web client connected: %s [%s], %d connected",
                 me.label, me.id, len(self.clients))
        jogging = set()
        last_jog_error = 0.0
        try:
            await ws.send_str(json.dumps({"type": "config", **config_summary(self.ctrl)}))
            await ws.send_str(json.dumps({"type": "presets",
                                          "presets": self.ctrl.presets.all()}))
            await ws.send_str(json.dumps({"type": "recordings",
                                          "recordings": self.ctrl.recorder.list()}))
            await ws.send_str(json.dumps({"type": "debug", "on": weblog.is_debug()}))
            await self.broadcast_clients()
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
                reply = await dispatch(self.ctrl, msg, source=me.label, client=me.id,
                                       local=me.ip in LOCAL_ADDRESSES)
                send_reply = "id" in msg or not reply["ok"]
                if not reply["ok"] and msg.get("cmd") == "jog":   # sent 25x/s: rate-limit
                    now = time.monotonic()
                    send_reply = now - last_jog_error > 1.0
                    if send_reply:
                        last_jog_error = now
                if send_reply:
                    reply.update(type="reply", id=msg.get("id"), cmd=msg.get("cmd"))
                    await ws.send_str(json.dumps(reply))
                if "presets" in reply:
                    await self.broadcast({"type": "presets", "presets": reply["presets"]})
                if "recordings" in reply:
                    await self.broadcast({"type": "recordings",
                                          "recordings": reply["recordings"]})
                if msg.get("cmd") == "debug":
                    await self.broadcast({"type": "debug", "on": weblog.is_debug()})
                if msg.get("cmd") in ("lock", "unlock"):
                    await self.broadcast_clients()
        finally:
            self.clients.pop(ws, None)
            self.log_clients.discard(ws)
            if jogging and self.ctrl.may_control(me.id):   # vanished while jogging
                self.ctrl.jog({k: 0.0 for k in jogging})
            if self.ctrl.lock_owner == me.id:
                self.ctrl.unlock(me.id)
                log.info("lock released: owner %s disconnected", me.label)
            log.info("web client disconnected: %s, %d connected", me.label, len(self.clients))
            await self.broadcast_clients()
        return ws
