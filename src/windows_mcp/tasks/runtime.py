"""Process-wide task runtime (store + event bus + screen lock) and its MCP middleware."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from windows_mcp.tasks.events import TaskEventBus
from windows_mcp.tasks.screen_lock import ScreenLock, ScreenLockTimeout, get_screen_lock
from windows_mcp.tasks.store import TaskStore

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on", "default"}


def default_state_path() -> Path:
    from platformdirs import user_data_dir

    return Path(user_data_dir("windows-mcp", appauthor=False)) / "tasks.json"


def _state_path_from_env() -> Path | None:
    raw = os.getenv("WINDOWS_MCP_TASKS_FILE", "").strip()
    if not raw or raw.lower() in {"0", "false", "no", "off"}:
        return None
    return default_state_path() if raw.lower() in _TRUE else Path(raw).expanduser()


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass
class TaskRuntime:
    store: TaskStore
    bus: TaskEventBus
    screen_lock: ScreenLock = field(default_factory=get_screen_lock)

    @classmethod
    def create(
        cls,
        *,
        state_path: str | Path | None = None,
        jsonl_path: str | Path | None = None,
        webhook_url: str | None = None,
        poster: Callable[..., Any] | None = None,
        screen_lock: ScreenLock | None = None,
    ) -> "TaskRuntime":
        bus = TaskEventBus(
            jsonl_path=jsonl_path,
            webhook_url=webhook_url,
            webhook_token=os.getenv("WINDOWS_MCP_TASK_WEBHOOK_TOKEN") or None,
            webhook_timeout=_float_env("WINDOWS_MCP_TASK_WEBHOOK_TIMEOUT", 2.0),
            poster=poster,
        )
        store = TaskStore(path=state_path, bus=bus)
        return cls(store=store, bus=bus, screen_lock=screen_lock or get_screen_lock())

    @classmethod
    def from_env(cls) -> "TaskRuntime":
        return cls.create(
            state_path=_state_path_from_env(),
            jsonl_path=os.getenv("WINDOWS_MCP_TASK_EVENTS_FILE") or None,
            webhook_url=os.getenv("WINDOWS_MCP_TASK_WEBHOOK") or None,
        )


_runtime: TaskRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> TaskRuntime:
    """The process-wide runtime, built from environment variables on first use."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = TaskRuntime.from_env()
        return _runtime


class OrchestrationMiddleware(Middleware):
    """Remember sessions for task-event pushes and serialize screen-touching tool calls.

    Install it outermost so the FIFO screen queue, not the control gate's polling lock, sets
    the order of screen calls. The lock is keyed on tool name and mode through
    ``control_policy.is_screen_call`` (the DIRECT set plus App switch/resize).
    """

    def __init__(self, runtime: TaskRuntime) -> None:
        self.runtime = runtime

    @staticmethod
    def _session(context: MiddlewareContext) -> Any:
        ctx = context.fastmcp_context
        if ctx is None:
            return None
        try:
            return ctx.session
        except RuntimeError:
            return None

    def _remember(self, context: MiddlewareContext, active: bool | None = None) -> None:
        session = self._session(context)
        if session is not None:
            self.runtime.bus.remember(session, active=active)

    async def on_initialize(self, context: MiddlewareContext, call_next: Callable) -> Any:
        result = await call_next(context)
        self._remember(context)
        return result

    async def on_list_tools(self, context: MiddlewareContext, call_next: Callable) -> Any:
        self._remember(context)
        return await call_next(context)

    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable) -> Any:
        self._remember(context, active=True)
        try:
            return await self._call_tool(context, call_next)
        finally:
            self._remember(context, active=False)

    async def _call_tool(self, context: MiddlewareContext, call_next: Callable) -> Any:
        from windows_mcp.tools.control_policy import is_screen_call

        name = context.message.name
        arguments = getattr(context.message, "arguments", None)
        if not is_screen_call(name, arguments):
            return await call_next(context)
        lock = self.runtime.screen_lock
        try:
            await lock.acquire(name)
        except ScreenLockTimeout as exc:
            raise ToolError(str(exc)) from None
        # Hold the lock until the tool really finishes. A cancelled request must not release
        # it while a worker thread is still typing or clicking.
        work = asyncio.ensure_future(call_next(context))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if work.done() and not work.cancelled():
                work.exception()  # Mark retrieved.
            raise
        finally:
            if work.done():
                lock.release()
            else:
                work.add_done_callback(lambda _: lock.release())
