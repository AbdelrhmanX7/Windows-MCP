"""Minimal Chrome DevTools Protocol client: one browser websocket, flat sessions per tab.

Synchronous on purpose: a reader thread matches responses to requests by id and buffers
events, so callers on any thread (the MCP worker threads, tests) can ``send`` and
``wait_event`` without binding the connection to one asyncio loop.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable

logger = logging.getLogger(__name__)

EVENT_BUFFER = 2000  # Events kept for wait_event(since=...) scans.


class CDPError(RuntimeError):
    """A protocol error reply (``{"error": {...}}``) or a request timeout."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class CDPTimeout(CDPError):
    pass


class CDPDisconnected(CDPError):
    """The websocket closed (browser exited or the debugging port went away)."""


class _Slot:
    __slots__ = ("event", "reply")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.reply: dict | None = None


def _ws_connect(url: str, timeout: float) -> Any:
    from websockets.sync.client import connect

    # No proxy (127.0.0.1), no compression (local, CPU only), no keepalive pings (a busy
    # renderer must not get the socket killed), no size cap (screenshots, big reads), and no
    # Origin header (Chromium rejects foreign origins without --remote-allow-origins).
    return connect(
        url,
        open_timeout=timeout,
        max_size=None,
        compression=None,
        proxy=None,
        ping_interval=None,
        close_timeout=1,
    )


class CDPConnection:
    """One websocket to the browser endpoint (``/json/version`` ``webSocketDebuggerUrl``)."""

    def __init__(
        self,
        ws_url: str,
        *,
        timeout: float = 5.0,
        connect: Callable[[str, float], Any] | None = None,
    ) -> None:
        self.url = ws_url
        self._ws = (connect or _ws_connect)(ws_url, timeout)
        self._ids = itertools.count(1)
        self._pending: dict[int, _Slot] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._cond = threading.Condition()
        self._events: deque[tuple[int, dict]] = deque(maxlen=EVENT_BUFFER)
        self._seq = 0
        self._listeners: list[Callable[[dict], None]] = []
        self.closed = False
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()

    # ------------------------------------------------------------------ reader
    def _read_loop(self) -> None:
        try:
            for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                mid = msg.get("id")
                if mid is not None:
                    with self._lock:
                        slot = self._pending.pop(mid, None)
                    if slot is not None:
                        slot.reply = msg
                        slot.event.set()
                    continue
                for listener in list(self._listeners):
                    try:
                        listener(msg)
                    except Exception:
                        logger.debug("CDP listener failed", exc_info=True)
                with self._cond:
                    self._seq += 1
                    self._events.append((self._seq, msg))
                    self._cond.notify_all()
        except Exception:
            logger.debug("CDP reader stopped", exc_info=True)
        finally:
            self.closed = True
            with self._lock:
                pending, self._pending = self._pending, {}
            for slot in pending.values():
                slot.event.set()
            with self._cond:
                self._cond.notify_all()

    # ------------------------------------------------------------------ requests
    def _post(self, method: str, params: dict | None, session: str | None) -> tuple[int, _Slot]:
        if self.closed:
            raise CDPDisconnected("browser connection is closed")
        mid = next(self._ids)
        slot = _Slot()
        with self._lock:
            self._pending[mid] = slot
        payload: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        try:
            with self._send_lock:
                self._ws.send(json.dumps(payload))
        except Exception as exc:
            with self._lock:
                self._pending.pop(mid, None)
            self.closed = True
            raise CDPDisconnected(f"browser connection lost: {exc}") from None
        return mid, slot

    def send(
        self,
        method: str,
        params: dict | None = None,
        *,
        session: str | None = None,
        timeout: float = 10.0,
    ) -> dict:
        """Send one command and wait for its reply; returns ``result`` or raises CDPError."""
        mid, slot = self._post(method, params, session)
        if not slot.event.wait(timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise CDPTimeout(f"{method} timed out after {timeout:g}s")
        reply = slot.reply
        if reply is None:
            raise CDPDisconnected(f"browser connection closed during {method}")
        if "error" in reply:
            err = reply["error"] or {}
            raise CDPError(str(err.get("message") or err), err.get("code"))
        return reply.get("result") or {}

    def send_nowait(self, method: str, params: dict | None = None, *, session: str | None = None):
        """Fire a command without waiting (safe from listeners on the reader thread)."""
        try:
            self._post(method, params, session)
        except CDPError:
            pass

    # ------------------------------------------------------------------ events
    def on_event(self, listener: Callable[[dict], None]) -> None:
        """Call *listener(msg)* on the reader thread for every event (never block in it)."""
        self._listeners.append(listener)

    def mark(self) -> int:
        """Sequence number of the latest buffered event; pass it as ``since``."""
        with self._cond:
            return self._seq

    def wait_event(
        self,
        predicate: Callable[[dict], bool],
        *,
        since: int,
        timeout: float,
    ) -> dict | None:
        """The first event after *since* matching *predicate*, waiting up to *timeout* s."""
        deadline = time.monotonic() + max(0.0, timeout)
        scanned = since
        with self._cond:
            while True:
                for seq, msg in self._events:
                    if seq <= scanned:
                        continue
                    scanned = seq
                    if predicate(msg):
                        return msg
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.closed:
                    return None
                self._cond.wait(remaining)

    def events_since(self, since: int) -> list[dict]:
        with self._cond:
            return [msg for seq, msg in self._events if seq > since]

    def close(self) -> None:
        self.closed = True
        try:
            self._ws.close()
        except Exception:
            pass
