"""Fan task events out to MCP sessions, a JSON-lines file and an optional webhook.

Every event gets a process-wide monotonic ``seq`` and a ``ts`` (Unix epoch milliseconds) so a
consumer can order and de-duplicate events that arrive over more than one channel. Delivery is
best effort and never blocks the caller: MCP pushes are scheduled onto the session's event loop,
the webhook runs on one daemon worker thread with a bounded queue, and a recent-history ring
buffer backs ``Tasks(mode="events")`` for clients that can only poll.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import queue
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

MCP_LOGGER_NAME = "windows-mcp.tasks"
MAX_EVENT_TEXT = 2000  # result/error/progress size cap inside one event.


def _clip_event(event: dict) -> dict:
    out = dict(event)
    for key in ("result", "error", "progress"):
        value = out.get(key)
        if value is None:
            continue
        text = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        )
        if len(text) > MAX_EVENT_TEXT:
            out[key] = text[:MAX_EVENT_TEXT] + "... [truncated]"
            out[f"{key}_truncated"] = True
    return out


@dataclass
class _SessionEntry:
    connection: Any
    session: Any  # Most recently seen per-request session façade for this connection.
    loop: asyncio.AbstractEventLoop
    last_seen: float
    active: list = field(default_factory=list)  # Sessions of in-flight requests.
    pending: collections.deque = field(default_factory=collections.deque)
    draining: bool = False

    def target(self) -> Any:
        # On the modern protocol, logs may only ride an in-flight request's stream; on the
        # handshake era any session of the connection can send standalone notifications.
        return self.active[-1] if self.active else self.session


def _connection_of(session: Any) -> Any:
    """FastMCP 4 builds one ServerSession per request; the connection is the stable identity."""
    return getattr(session, "_connection", None) or session


class TaskEventBus:
    """Publish task/plan events to every configured sink."""

    def __init__(
        self,
        *,
        jsonl_path: str | Path | None = None,
        webhook_url: str | None = None,
        webhook_token: str | None = None,
        webhook_timeout: float = 2.0,
        history: int = 500,
        poster: Callable[..., Any] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._history: collections.deque[dict] = collections.deque(maxlen=history)
        self._listeners: list[Callable[[dict], None]] = []
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        self.webhook_url = webhook_url or None
        self.webhook_token = webhook_token or None
        self.webhook_timeout = webhook_timeout
        self._poster = poster
        self._webhook_queue: queue.Queue | None = None
        self._webhook_thread: threading.Thread | None = None
        self._sessions: dict[int, _SessionEntry] = {}
        self.ttl = float("inf")
        self.push_enabled = True
        self.webhook_dropped = 0

    # ------------------------------------------------------------------ configuration
    def configure_transport(self, transport: str, *, stateless_http: bool = False) -> None:
        """Mirror the control notifier: stdio sessions live forever, HTTP ones expire."""
        self.ttl = float("inf") if transport == "stdio" else 60.0
        self.push_enabled = not (transport == "streamable-http" and stateless_http)

    def subscribe(self, listener: Callable[[dict], None]) -> None:
        """Register an in-process listener (called synchronously, must not block)."""
        self._listeners.append(listener)

    # ------------------------------------------------------------------ sessions
    def remember(self, session: Any, *, active: bool | None = None) -> None:
        """Track an MCP session seen in a real request; call from that session's event loop.

        ``active=True`` marks the start of a request on that session and ``active=False``
        its end, so events emitted meanwhile ride that request's stream.
        """
        if not self.push_enabled or session is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        connection = _connection_of(session)
        key = id(connection)
        with self._lock:
            entry = self._sessions.get(key)
            if entry is None or entry.connection is not connection or entry.loop is not loop:
                entry = _SessionEntry(connection, session, loop, time.monotonic())
                self._sessions[key] = entry
            entry.last_seen = time.monotonic()
            if active is None:
                entry.session = session
            elif active:
                entry.session = session
                entry.active.append(session)
            else:
                try:
                    entry.active.remove(session)
                except ValueError:
                    pass

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    # ------------------------------------------------------------------ publish
    def publish(self, event: dict) -> dict:
        """Stamp *event* with seq/ts and deliver it. Returns the stamped event."""
        with self._lock:
            self._seq += 1
            stamped = _clip_event({**event, "seq": self._seq, "ts": int(time.time() * 1000)})
            self._history.append(stamped)
            self._write_jsonl(stamped)
            sessions = self._live_sessions()
        for listener in list(self._listeners):
            try:
                listener(stamped)
            except Exception:
                logger.debug("Task event listener failed", exc_info=True)
        self._enqueue_webhook(stamped)
        for key, entry in sessions:
            try:
                entry.loop.call_soon_threadsafe(self._enqueue_session, key, entry, stamped)
            except RuntimeError:  # Loop closed between the check and scheduling.
                with self._lock:
                    self._sessions.pop(key, None)
        return stamped

    def history(self, since: int = 0, plan_id: str | None = None, limit: int = 200) -> list[dict]:
        with self._lock:
            events = [e for e in self._history if e["seq"] > since]
        if plan_id is not None:
            events = [e for e in events if e.get("plan_id") == plan_id]
        return events[-limit:] if limit > 0 else events

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    # ------------------------------------------------------------------ sinks
    def _write_jsonl(self, event: dict) -> None:
        if self.jsonl_path is None:
            return
        try:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logger.debug("Could not append task event to %s", self.jsonl_path, exc_info=True)

    def _live_sessions(self) -> list[tuple[int, _SessionEntry]]:
        cutoff = time.monotonic() - self.ttl
        live = []
        for key, entry in tuple(self._sessions.items()):
            if entry.last_seen < cutoff or entry.loop.is_closed():
                self._sessions.pop(key, None)
            else:
                live.append((key, entry))
        return live

    def _enqueue_session(self, key: int, entry: _SessionEntry, event: dict) -> None:
        # Runs on the session's loop. One drain task per session keeps delivery ordered.
        entry.pending.append(event)
        if not entry.draining:
            entry.draining = True
            asyncio.get_running_loop().create_task(self._drain(key, entry))

    async def _drain(self, key: int, entry: _SessionEntry) -> None:
        try:
            while entry.pending:
                event = entry.pending.popleft()
                try:
                    with warnings.catch_warnings():
                        # MCP logging is deprecated (SEP-2577) but still the only push channel
                        # every client handles; ControlNotifier uses it the same way.
                        warnings.simplefilter("ignore")
                        await entry.target().send_log_message(
                            level="notice", data=event, logger=MCP_LOGGER_NAME
                        )
                except Exception:
                    logger.debug("Task event push failed; session dropped", exc_info=True)
                    entry.pending.clear()
                    with self._lock:
                        if self._sessions.get(key) is entry:
                            self._sessions.pop(key, None)
                    return
        finally:
            entry.draining = False

    def _enqueue_webhook(self, event: dict) -> None:
        if not self.webhook_url:
            return
        if self._webhook_thread is None:
            with self._lock:
                if self._webhook_thread is None:
                    self._webhook_queue = queue.Queue(maxsize=1000)
                    self._webhook_thread = threading.Thread(
                        target=self._webhook_worker, name="wmcp-task-webhook", daemon=True
                    )
                    self._webhook_thread.start()
        try:
            self._webhook_queue.put_nowait(event)
        except queue.Full:
            self.webhook_dropped += 1

    def _webhook_worker(self) -> None:
        post = self._poster
        if post is None:
            import requests

            post = requests.post
        headers = {"Content-Type": "application/json"}
        if self.webhook_token:
            headers["Authorization"] = f"Bearer {self.webhook_token}"
        assert self._webhook_queue is not None
        while True:
            event = self._webhook_queue.get()
            try:
                post(self.webhook_url, json=event, headers=headers, timeout=self.webhook_timeout)
            except Exception:
                logger.debug("Task webhook POST failed", exc_info=True)
            finally:
                self._webhook_queue.task_done()

    def flush_webhook(self, timeout: float = 5.0) -> bool:
        """Wait until queued webhook posts are sent (tests and shutdown). True when drained."""
        q = self._webhook_queue
        if q is None:
            return True
        deadline = time.monotonic() + timeout
        while q.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True
