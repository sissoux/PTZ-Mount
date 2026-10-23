"""Python client for the PTZ mount (WebSocket API).

Copy this single file next to your script; it only needs `aiohttp`
(pip install aiohttp). Full protocol reference: docs/api.md.

    import asyncio
    from ptz_client import PTZClient

    async def main():
        async with PTZClient("PTZMount.local") as ptz:
            await ptz.lock()                          # nobody else can move the head
            await ptz.home(wait=True)
            await ptz.goto(pan=30, tilt=-5, wait=True)
            print(ptz.position("pan"))

    asyncio.run(main())

Every command method returns the reply dict and raises PTZError when the
mount refuses it (reply "ok": false), e.g. "axis 'pan' is not homed".
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import secrets
from typing import Any, Callable, Dict, Optional

import aiohttp

log = logging.getLogger("ptz_client")

__all__ = ["PTZClient", "PTZError", "JogStream"]


class PTZError(Exception):
    """The mount refused a command (reply with "ok": false)."""

    def __init__(self, cmd: str, error: str):
        super().__init__(f"{cmd}: {error}")
        self.cmd, self.error = cmd, error


class PTZClient:
    """WebSocket client. Use as an async context manager or call
    connect() / close() yourself.

    token: identifies this script for the blocking mode (lock). Keep the same
    token across reconnections to keep ownership; 8-40 letters / digits.
    reconnect: on a lost connection, reconnect automatically (and take the
    lock again if this client held it).
    """

    def __init__(self, host: str = "PTZMount.local", port: int = 8080,
                 token: Optional[str] = None, reconnect: bool = True,
                 timeout: float = 5.0):
        self.url = f"http://{host}:{port}/ws"
        self.token = token or "script" + secrets.token_hex(8)
        self.reconnect = reconnect
        self.timeout = timeout
        self.status: Dict[str, Any] = {}            # latest status push
        self.config: Dict[str, Any] = {}
        self.presets: Dict[str, Any] = {}
        self.recordings: list = []
        self.on_status: Optional[Callable[[dict], None]] = None
        self.on_message: Optional[Callable[[dict], None]] = None   # every push
        self._ids = itertools.count(1)
        self._pending: Dict[int, asyncio.Future] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._reader: Optional[asyncio.Task] = None
        self._status_event: Optional[asyncio.Event] = None   # created in connect()
        self._connected: Optional[asyncio.Event] = None      # (bound to the running loop)
        self._closing = False
        self._want_lock = False

    # ------------------------------------------------------------ connection
    async def __aenter__(self) -> "PTZClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        self._status_event, self._connected = asyncio.Event(), asyncio.Event()
        if self._session is None:
            self._session = aiohttp.ClientSession()
        await self._open()
        self._reader = asyncio.get_running_loop().create_task(self._read_loop())
        await self.wait_status(lambda s: True, timeout=self.timeout)

    async def _open(self) -> None:
        self._ws = await self._session.ws_connect(f"{self.url}?token={self.token}",
                                                  heartbeat=5.0)
        self._connected.set()
        log.info("connected to %s", self.url)

    async def close(self) -> None:
        self._closing = True
        if self._reader:
            self._reader.cancel()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
        self._fail_pending(ConnectionError("client closed"))

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def _read_loop(self) -> None:
        while not self._closing:
            try:
                async for m in self._ws:
                    if m.type != aiohttp.WSMsgType.TEXT:
                        continue
                    self._dispatch(json.loads(m.data))
            except Exception as e:  # noqa: BLE001
                log.warning("connection error: %s", e)
            self._connected.clear()
            self._fail_pending(ConnectionError("connection lost"))
            if self._closing or not self.reconnect:
                return
            log.warning("connection lost, reconnecting...")
            while not self._closing:
                try:
                    await asyncio.sleep(1.0)
                    await self._open()
                    if self._want_lock:     # not awaited here: this task reads the reply
                        asyncio.get_running_loop().create_task(self._relock())
                    break
                except Exception as e:  # noqa: BLE001
                    log.debug("reconnect failed: %s", e)

    async def _relock(self) -> None:
        try:
            await self.call("lock")
            log.info("lock taken again after reconnection")
        except Exception as e:  # noqa: BLE001
            log.warning("could not take the lock again: %s", e)

    def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "reply":
            fut = self._pending.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            elif not msg.get("ok"):      # rejected jog (no id): log it
                log.warning("%s rejected: %s", msg.get("cmd"), msg.get("error"))
        elif t == "status":
            self.status = msg
            self._status_event.set()
            if self.on_status:
                self.on_status(msg)
        elif t == "config":
            self.config = msg
        elif t == "presets":
            self.presets = msg.get("presets", {})
        elif t == "recordings":
            self.recordings = msg.get("recordings", [])
        if self.on_message:
            self.on_message(msg)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ------------------------------------------------------------ low level
    async def send(self, cmd: str, **params) -> None:
        """Fire and forget (no reply unless refused). Used for jog."""
        await self._connected.wait()
        await self._ws.send_str(json.dumps({"cmd": cmd, **params}))

    async def call(self, cmd: str, timeout: Optional[float] = None, **params) -> dict:
        """Send a command and wait for its reply. Raises PTZError if refused.
        With wait=True the reply only comes when the motion is finished, so
        pass a timeout long enough for it."""
        await asyncio.wait_for(self._connected.wait(), timeout or self.timeout)
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._ws.send_str(json.dumps({"cmd": cmd, "id": rid, **params}))
        if timeout is None:
            timeout = 3600.0 if params.get("wait") else self.timeout
        try:
            reply = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)
        if not reply.get("ok"):
            raise PTZError(cmd, reply.get("error", "refused"))
        return reply

    async def wait_status(self, predicate: Callable[[dict], bool],
                          timeout: Optional[float] = None) -> dict:
        """Wait until a status push satisfies predicate(status)."""
        async def _wait():
            while True:
                if self.status and predicate(self.status):
                    return self.status
                self._status_event.clear()
                await self._status_event.wait()
        return await asyncio.wait_for(_wait(), timeout)

    # ------------------------------------------------------------ status helpers
    def position(self, axis: str) -> float:
        return self.status["axes"][axis]["pos"]

    def positions(self) -> Dict[str, float]:
        return {n: a["pos"] for n, a in self.status.get("axes", {}).items()}

    def is_homed(self, *axes: str) -> bool:
        ax = self.status.get("axes", {})
        names = axes or [n for n, a in ax.items() if a.get("active", True)]
        return all(ax[n]["homed"] for n in names)

    # ------------------------------------------------------------ commands
    async def lock(self) -> dict:
        """Blocking mode: only this client can move the head (Stop / E-stop
        stay open to everyone, and track_go stays open to the timing system)."""
        self._want_lock = True
        return await self.call("lock")

    async def unlock(self) -> dict:
        self._want_lock = False
        return await self.call("unlock")

    async def home(self, axes=None, wait: bool = True, timeout: float = 120) -> dict:
        params = {"wait": wait}
        if axes:
            params["axes"] = list(axes)
        return await self.call("home", timeout=timeout if wait else None, **params)

    async def goto(self, speed: float = 1.0, wait: bool = False,
                   timeout: Optional[float] = None, **axes: float) -> dict:
        """Absolute move, e.g. goto(pan=30, tilt=-5, wait=True).
        speed: fraction (0..1) of the current speed setting."""
        return await self.call("goto", timeout=timeout, speed=speed, wait=wait, **axes)

    async def move_rel(self, speed: float = 1.0, wait: bool = False,
                       timeout: Optional[float] = None, **deltas: float) -> dict:
        return await self.call("move_rel", timeout=timeout, speed=speed, wait=wait, **deltas)

    async def preset(self, number: int, speed: float = 1.0, wait: bool = False,
                     timeout: Optional[float] = None) -> dict:
        return await self.call("preset_recall", timeout=timeout, preset=number,
                               speed=speed, wait=wait)

    async def save_preset(self, number: int, name: str = "") -> dict:
        return await self.call("preset_save", preset=number, name=name)

    async def jog(self, **values: float) -> None:
        """One jog frame, values -1..1 (pan=, tilt=, zoom=). Must be repeated
        at least every jog_timeout (0.5 s): use JogStream for continuous jog."""
        await self.send("jog", **values)

    async def stop(self) -> dict:
        return await self.call("stop")

    async def estop(self) -> dict:
        return await self.call("estop")

    async def clear_estop(self) -> dict:
        return await self.call("clear_estop")

    async def set_motion(self, **settings) -> dict:
        """speed=, accel= (units/s, units/s^2), smoothing= (0..1), advanced=,
        axis_speed={...}, axis_accel={...}"""
        return await self.call("set_motion", **settings)

    async def axis_active(self, axis: str, on: bool) -> dict:
        return await self.call("axis_active", axis=axis, on=on)

    # race tracking
    async def track_arm(self, name: str, wait: bool = True, timeout: float = 120) -> dict:
        return await self.call("track_arm", timeout=timeout if wait else None,
                               name=name, wait=wait)

    async def track_go(self) -> dict:
        return await self.call("track_go")

    async def wait_lap(self, timeout: Optional[float] = None) -> dict:
        """After track_go: wait until that lap is finished (or tracking stops).
        A new GO during the lap also counts as the end of this lap."""
        runs = self.status["tracking"]["runs"]
        return await self.wait_status(
            lambda s: s["tracking"]["runs"] > runs or s["tracking"]["phase"] == "idle", timeout)

    async def track_set(self, **settings) -> dict:
        """target_lap=, speed=, auto_rearm=, auto_adjust=, adjust_tolerance="""
        return await self.call("track_set", **settings)

    async def track_abort(self) -> dict:
        return await self.call("track_abort")

    # recordings
    async def play(self, name: str, speed: float = 1.0, loop: bool = False,
                   wait: bool = False, timeout: Optional[float] = None) -> dict:
        return await self.call("play", timeout=timeout, name=name, speed=speed,
                               loop=loop, wait=wait)

    async def play_stop(self) -> dict:
        return await self.call("play_stop")


class JogStream:
    """Continuous jog: keeps sending the current values at `rate` Hz (the
    mount stops a jog that is not refreshed within jog_timeout).

        async with JogStream(ptz) as jog:
            jog.set(pan=0.3)            # starts moving
            await asyncio.sleep(2)
            jog.set(pan=0, tilt=-0.5)
        # leaving the block sends zeros (smooth stop)
    """

    def __init__(self, client: PTZClient, rate: float = 25.0):
        self.client, self.period = client, 1.0 / rate
        self.values: Dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None

    def set(self, **values: float) -> None:
        self.values.update({k: max(-1.0, min(1.0, float(v))) for k, v in values.items()})

    async def __aenter__(self) -> "JogStream":
        self._task = asyncio.get_running_loop().create_task(self._run())
        return self

    async def __aexit__(self, *exc) -> None:
        self._task.cancel()
        self.values = {k: 0.0 for k in self.values}
        if self.values and self.client.connected:
            await self.client.jog(**self.values)

    async def _run(self) -> None:
        while True:
            if any(self.values.values()):
                try:
                    await self.client.jog(**self.values)
                except Exception as e:  # noqa: BLE001
                    log.debug("jog not sent: %s", e)
            await asyncio.sleep(self.period)
