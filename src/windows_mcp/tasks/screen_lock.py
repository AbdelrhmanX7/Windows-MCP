"""A process-wide, FIFO-fair screen lock so screen-touching tool calls never interleave.

The lock is thread-safe and loop-agnostic: waiters park on a future of their own event loop
and the releaser hands ownership directly to the oldest waiter (no barging), waking it with
``call_soon_threadsafe``. A waiter that times out or is cancelled leaves the queue; if it was
granted the lock at the same instant, it releases it again before raising.
"""

from __future__ import annotations

import asyncio
import collections
import os
import threading
import time
from dataclasses import dataclass, field

DEFAULT_TIMEOUT = 60.0


def default_timeout() -> float:
    raw = os.getenv("WINDOWS_MCP_SCREEN_LOCK_TIMEOUT", "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


class ScreenLockTimeout(TimeoutError):
    """Raised when the screen lock is not granted within the timeout."""


@dataclass
class _Waiter:
    owner: str
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future
    enqueued: float = field(default_factory=time.monotonic)
    granted: bool = False


class ScreenLock:
    """FIFO ticket lock shared by every event loop and thread in the process."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._waiters: collections.deque[_Waiter] = collections.deque()
        self._holder: str | None = None
        self._held_since: float | None = None
        self._grants = 0

    def status(self) -> dict:
        with self._mutex:
            return {
                "holder": self._holder,
                "held_for_s": round(time.monotonic() - self._held_since, 3)
                if self._held_since is not None
                else None,
                "queued": [w.owner for w in self._waiters],
                "grants": self._grants,
            }

    @property
    def locked(self) -> bool:
        with self._mutex:
            return self._holder is not None

    async def acquire(self, owner: str, timeout: float | None = None) -> None:
        """Wait (in FIFO order) for the lock. Raises ScreenLockTimeout on timeout."""
        timeout = default_timeout() if timeout is None else timeout
        loop = asyncio.get_running_loop()
        with self._mutex:
            if self._holder is None and not self._waiters:
                self._grant(owner)
                return
            waiter = _Waiter(owner, loop, loop.create_future())
            self._waiters.append(waiter)
            holder, queued = self._holder, len(self._waiters)
        try:
            await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
        except BaseException as exc:
            with self._mutex:
                granted = waiter.granted
                if not granted:
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass
            if granted:
                self.release()  # Granted just as we gave up: pass it on.
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                raise ScreenLockTimeout(
                    f"SCREEN_BUSY: {owner} waited {timeout:g}s for the screen lock "
                    f"(held by {holder}, {queued - 1} call(s) ahead in queue). Screen tools "
                    "run one at a time; retry, or raise WINDOWS_MCP_SCREEN_LOCK_TIMEOUT."
                ) from None
            raise

    def _grant(self, owner: str) -> None:
        self._holder = owner
        self._held_since = time.monotonic()
        self._grants += 1

    def release(self) -> None:
        with self._mutex:
            if self._holder is None:
                raise RuntimeError("screen lock released while not held")
            self._holder = None
            self._held_since = None
            while self._waiters:
                waiter = self._waiters.popleft()
                if waiter.loop.is_closed():
                    continue
                waiter.granted = True
                self._grant(waiter.owner)
                try:
                    waiter.loop.call_soon_threadsafe(_wake, waiter.future)
                except RuntimeError:  # Loop closed after the check.
                    waiter.granted = False
                    self._holder = None
                    self._held_since = None
                    continue
                return


def _wake(future: asyncio.Future) -> None:
    if not future.done():
        future.set_result(True)


_SCREEN_LOCK = ScreenLock()


def get_screen_lock() -> ScreenLock:
    return _SCREEN_LOCK
