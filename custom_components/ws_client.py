from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import time
from typing import Any, Awaitable, Callable, Optional

import websockets  # use HA-bundled version (>=15)

from .const import (
    RETRY_MIN_BACKOFF,
    RETRY_MAX_BACKOFF,
    HEARTBEAT_SECS,
    PROBE_ON_SILENCE_SECS,
)

_LOGGER = logging.getLogger(__name__)
OnMessage = Callable[[dict[str, Any]], Awaitable[None]]

# Periodic “get” cadences (mirror browser behavior)
GET_REQPRINTERPARA_SEC = 5.0         # curPosition, autohome, etc.
GET_PRINT_OBJECTS_SEC = 2.0          # objects/exclusions/current object


def _coerce_numbers(d: dict[str, Any]) -> dict[str, Any]:
    """Convert numeric strings from the printer to actual numbers for HA."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            try:
                out[k] = float(v) if "." in v else int(v)
                continue
            except Exception:
                pass
        out[k] = v
    return out


class K1CClient:
    """Resilient WS client with backoff, heartbeat 'ok', periodic GETs, and staleness tracking."""

    def __init__(self, host: str, on_message: OnMessage):
        self._host = host
        self._url = lambda: f"ws://{self._resolve_host()}:{9999}"
        self._on_message = on_message
        self._state: dict[str, Any] = {}

        self._task: Optional[asyncio.Task] = None
        self._ws: Optional[websockets.client.ClientConnection] = None  # type: ignore[attr-defined]
        self._stop = asyncio.Event()
        self._connected_once = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._last_rx = 0.0

        self._hb_task: Optional[asyncio.Task] = None
        self._tick_task: Optional[asyncio.Task] = None

        # NEW: event that indicates a live socket is present
        self._ws_ready = asyncio.Event()

    # ---------- lifecycle ----------
    async def start(self) -> None:
        if self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="k1c-ws-loop")

    async def stop(self) -> None:
        self._stop.set()
        for t in (self._hb_task, self._tick_task):
            if t:
                t.cancel()
        ws = self._ws
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None

    async def wait_first_connect(self, timeout: float = 5.0) -> bool:
        try:
            await asyncio.wait_for(self._connected_once.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_connected(self, timeout: float) -> bool:
        """Wait for a live WebSocket connection (used by retrying sender)."""
        try:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------- connectivity loop ----------
    def _resolve_host(self) -> str:
        try:
            return socket.gethostbyname(self._host)
        except Exception:
            return self._host

    async def _loop(self) -> None:
        backoff = RETRY_MIN_BACKOFF
        while not self._stop.is_set():
            try:
                url = self._url()
                _LOGGER.debug("K1C WS connecting: %s", url)
                # Disable library pings; we do app-level heartbeat + periodic GETs.
                async with websockets.connect(url, ping_interval=None) as ws:
                    self._ws = ws
                    self._ws_ready.set()  # signal connected
                    _LOGGER.info("K1C WS connected to %s", url)
                    self._connected_once.set()
                    backoff = RETRY_MIN_BACKOFF
                    self._last_rx = time.monotonic()

                    # background tasks
                    self._hb_task = asyncio.create_task(self._heartbeat(), name="k1c-ws-heartbeat")
                    self._tick_task = asyncio.create_task(self._periodic_gets(), name="k1c-ws-ticker")

                    async for raw in ws:
                        self._last_rx = time.monotonic()
                        # websockets>=15: text is str, binary is bytes
                        if isinstance(raw, (bytes, bytearray)):
                            text = raw.decode("utf-8", "ignore")
                        else:
                            text = raw

                        # Fast-path: if it's exactly "ok", ignore
                        if text == "ok":
                            continue

                        # Try parse JSON
                        try:
                            payload: Any = json.loads(text)
                        except Exception:
                            # Not JSON; ignore
                            continue

                        # Heartbeat handling
                        if isinstance(payload, dict) and payload.get("ModeCode") == "heart_beat":
                            # ACK immediately; literal 'ok' (no JSON)
                            try:
                                await ws.send("ok")
                            except Exception:
                                pass
                            continue

                        if isinstance(payload, dict):
                            merged = _coerce_numbers(payload)
                            self._state.update(merged)
                            try:
                                await self._on_message(dict(self._state))
                            except Exception:
                                _LOGGER.exception("K1C on_message failed")
                        else:
                            _LOGGER.debug("K1C WS unexpected frame type: %r", type(payload))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                _LOGGER.warning("K1C WS connection error: %s", exc)
            finally:
                # cleanup on disconnect
                for t in (self._hb_task, self._tick_task):
                    if t:
                        t.cancel()
                self._hb_task = self._tick_task = None

                self._ws = None
                self._ws_ready.clear()

            # exponential backoff with jitter
            jitter = random.uniform(0.0, 0.4)
            sleep_for = min(backoff * (1.8 + jitter), RETRY_MAX_BACKOFF)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass
            backoff = min(sleep_for, RETRY_MAX_BACKOFF)

        _LOGGER.debug("K1C WS loop exited")

    async def _heartbeat(self):
        """Benign probe on silent connects and a WS-level ping keeps NAT/state alive."""
        try:
            await asyncio.sleep(PROBE_ON_SILENCE_SECS)
            if time.monotonic() - self._last_rx > PROBE_ON_SILENCE_SECS:
                try:
                    await self._send_json({"method": "get", "params": {"ReqPrinterPara": 1}})
                except Exception:
                    pass

            while True:
                await asyncio.sleep(HEARTBEAT_SECS)
                ws = self._ws
                if not ws:
                    break
                try:
                    pong = await ws.ping()
                    await asyncio.wait_for(pong, timeout=5.0)
                except Exception:
                    _LOGGER.debug("K1C WS ping failed; forcing reconnect")
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    break
        except asyncio.CancelledError:
            return

    async def _periodic_gets(self):
        """Mirror the web UI's periodic GETs so the printer keeps streaming state."""
        try:
            t_para = 0.0
            t_objs = 0.0
            # Staggered loop to avoid bursts
            while True:
                now = time.monotonic()
                ws = self._ws
                if not ws:
                    break

                if now - t_para >= GET_REQPRINTERPARA_SEC:
                    try:
                        await self._send_json({"method": "get", "params": {"ReqPrinterPara": 1}})
                    except Exception:
                        pass
                    t_para = now

                if now - t_objs >= GET_PRINT_OBJECTS_SEC:
                    try:
                        await self._send_json({"method": "get", "params": {"reqPrintObjects": 1}})
                    except Exception:
                        pass
                    t_objs = now

                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            return

    # ---------- public send ----------
    async def send_set(self, **params: Any) -> None:
        """Single-attempt sender (kept for internal use)."""
        await self._send_json({"method": "set", "params": params})

    async def send_set_retry(self, *, wait_reconnect: float = 6.0, **params: Any) -> None:
        """
        Robust sender for user actions: try once; if the link recycled,
        wait for reconnect and retry once.
        """
        try:
            await self._send_json({"method": "set", "params": params})
            return
        except Exception as first_exc:
            ok = await self.wait_connected(wait_reconnect)
            if not ok:
                raise RuntimeError(
                    f"printer link not available after {wait_reconnect}s"
                ) from first_exc
            await self._send_json({"method": "set", "params": params})

    async def _send_json(self, obj: dict[str, Any]) -> None:
        async with self._send_lock:
            ws = self._ws
            if not ws:
                raise RuntimeError("WebSocket not connected")
            await ws.send(json.dumps(obj, separators=(",", ":")))

    # ---------- health ----------
    def last_rx_monotonic(self) -> float:
        return self._last_rx
