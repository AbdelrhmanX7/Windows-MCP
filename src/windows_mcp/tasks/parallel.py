"""RunParallel executor: background calls run concurrently, screen calls run in order.

Every inner call is dispatched through the server's own ``call_tool`` path, so the control
gate, policy, screen lock, analytics and argument validation apply to each one exactly as
they would to a direct MCP request.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from windows_mcp.tasks.screen_lock import ScreenLockTimeout
from windows_mcp.tasks.store import TaskError, TaskStore

DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_TIMEOUT = 60.0
MAX_CALLS = 64


def _env_number(name: str, default: float, cast: type) -> Any:
    try:
        value = cast(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def default_max_concurrency() -> int:
    return min(16, _env_number("WINDOWS_MCP_PARALLEL_MAX", DEFAULT_MAX_CONCURRENCY, int))


def default_timeout() -> float:
    return _env_number("WINDOWS_MCP_PARALLEL_TIMEOUT", DEFAULT_TIMEOUT, float)


@dataclass
class Call:
    index: int
    tool: str
    args: dict
    lane: str
    task_id: str | None = None
    plan_id: str | None = None
    status: str = "pending"  # ok, error, timeout, skipped
    result: str | None = None
    error: str | None = None
    started_ms: float | None = None
    latency_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "tool": self.tool,
            "lane": self.lane,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "ok": self.status == "ok",
            "status": self.status,
            "latency_ms": self.latency_ms,
            "started_ms": self.started_ms,
            "result": self.result,
            "error": self.error,
        }


def parse_calls(raw: Any, *, default_plan: str | None) -> list[Call]:
    from windows_mcp.tools.control_policy import is_screen_call

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"calls must be a JSON list: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("calls must be a non-empty list of {tool, args, task_id?} objects")
    if len(raw) > MAX_CALLS:
        raise ValueError(f"at most {MAX_CALLS} calls per RunParallel")
    calls = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"calls[{index}] must be an object")
        unknown = set(item) - {"tool", "args", "task_id", "plan_id", "lane"}
        if unknown:
            raise ValueError(f"calls[{index}] has unknown fields: {sorted(unknown)}")
        tool = item.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ValueError(f"calls[{index}].tool must be a tool name")
        if tool == "RunParallel":
            raise ValueError("RunParallel cannot be nested")
        args = item.get("args") or {}
        if isinstance(args, str):
            args = json.loads(args)
        if not isinstance(args, dict):
            raise ValueError(f"calls[{index}].args must be an object")
        lane_override = item.get("lane")
        if lane_override not in (None, "screen", "background"):
            raise ValueError(f"calls[{index}].lane must be 'screen' or 'background'")
        # Screen tools always use the screen lane; a call may only opt *into* it
        # (e.g. an App launch that must finish taking focus before the next Type).
        lane = "screen" if is_screen_call(tool, args) or lane_override == "screen" else "background"
        task_id = item.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            raise ValueError(f"calls[{index}].task_id must be a string")
        plan_id = item.get("plan_id", default_plan)
        calls.append(Call(index, tool, args, lane, task_id, plan_id))
    return calls


def result_text(result: Any, limit: int) -> str:
    """Flatten a ToolResult's content blocks to text."""
    parts = []
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append(block.text)
        elif kind == "image":
            parts.append(
                f"[{getattr(block, 'mimeType', 'image')} image omitted; call the tool directly to see it]"
            )
        else:
            parts.append(f"[{kind} content omitted]")
    if not parts and getattr(result, "structured_content", None) is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False, default=str))
    text = "\n".join(parts)
    if len(text) > limit:
        text = text[:limit] + f"... [truncated {len(text) - limit} chars]"
    return text


class _TaskLinker:
    """Mirror call outcomes onto linked tasks (running on first start, done/failed on last end)."""

    def __init__(self, store: TaskStore | None, calls: list[Call]) -> None:
        self.store = store
        self.remaining: dict[tuple[str | None, str], int] = {}
        self.outcomes: dict[tuple[str | None, str], list[Call]] = {}
        self.keys: dict[int, tuple[str | None, str]] = {}
        if store is None:
            return
        for call in calls:
            if call.task_id is None:
                continue
            plan, task = store.resolve(call.plan_id, call.task_id)  # Raises TaskError early.
            key = (plan.id, task.id)
            call.plan_id = plan.id
            self.keys[call.index] = key
            self.remaining[key] = self.remaining.get(key, 0) + 1
            self.outcomes.setdefault(key, [])

    def cancelled(self, call: Call) -> bool:
        key = self.keys.get(call.index)
        if key is None or self.store is None:
            return False
        try:
            return self.store.snapshot_task(*key)["status"] == "cancelled"
        except TaskError:
            return True

    def start(self, call: Call) -> None:
        key = self.keys.get(call.index)
        if key is None or self.store is None:
            return
        try:
            status = self.store.snapshot_task(*key)["status"]
            if status in ("pending", "failed"):
                self.store.update(*key, status="running", progress=f"running {call.tool}")
            elif status == "running":
                self.store.update(*key, progress=f"running {call.tool}")
        except TaskError:
            pass

    def finish(self, call: Call) -> None:
        key = self.keys.get(call.index)
        if key is None or self.store is None:
            return
        self.outcomes[key].append(call)
        self.remaining[key] -= 1
        if self.remaining[key] > 0:
            return
        done = self.outcomes[key]
        failed = [c for c in done if c.status != "ok"]
        try:
            status = self.store.snapshot_task(*key)["status"]
            if status in ("done", "skipped", "cancelled"):
                return  # The planner (or a cancel) already settled it.
            if failed:
                self.store.update(
                    *key,
                    status="failed",
                    error="; ".join(f"{c.tool}: {c.error}" for c in failed),
                    progress=f"{len(done) - len(failed)}/{len(done)} calls succeeded",
                )
            else:
                result = done[-1].result if len(done) == 1 else [c.result for c in done]
                self.store.update(*key, status="done", result=result, progress="done")
        except TaskError:
            pass


async def run_parallel(
    call_tool: Callable[[str, dict], Awaitable[Any]],
    calls: list[Call],
    *,
    store: TaskStore | None = None,
    max_concurrency: int | None = None,
    timeout: float | None = None,
    stop_screen_on_error: bool = True,
    max_result_chars: int = 4000,
    screen_lock: Any = None,
) -> dict:
    """Run *calls*; returns a summary dict with per-call results in input order."""
    from windows_mcp.tools.control_policy import is_screen_call

    max_concurrency = max(1, min(16, max_concurrency or default_max_concurrency()))
    timeout = timeout if timeout and timeout > 0 else default_timeout()
    linker = _TaskLinker(store, calls)
    semaphore = asyncio.Semaphore(max_concurrency)
    t0 = time.perf_counter()

    async def execute(call: Call, *, bounded: bool) -> None:
        if linker.cancelled(call):
            call.status, call.error = "skipped", "linked task was cancelled"
            return
        linker.start(call)
        start = time.perf_counter()
        call.started_ms = round((start - t0) * 1000, 1)
        try:
            if bounded:
                # Background calls: the timeout stops waiting, but a tool already running in a
                # worker thread finishes on its own; its result is discarded.
                result = await asyncio.wait_for(call_tool(call.tool, call.args), timeout)
            elif screen_lock is not None and not is_screen_call(call.tool, call.args):
                # Opted into the screen lane (e.g. an App launch before a Type): the
                # middleware does not lock it, so hold the screen lock here. A concurrent
                # request's Click cannot land between this launch and the next Type.
                await screen_lock.acquire(call.tool)
                try:
                    result = await call_tool(call.tool, call.args)
                finally:
                    screen_lock.release()
            else:
                # Screen calls: never abandon a call mid-input (the screen lock would be
                # released while keys are still being sent); the lock wait has its own timeout.
                result = await call_tool(call.tool, call.args)
            call.status = "ok"
            call.result = result_text(result, max_result_chars)
        except ScreenLockTimeout as exc:
            call.status, call.error = "error", str(exc)
        except asyncio.TimeoutError:
            call.status, call.error = "timeout", f"timed out after {timeout:g}s"
        except Exception as exc:  # ToolError, NotFoundError, ValidationError, ...
            call.status = "error"
            call.error = str(exc) or type(exc).__name__
        finally:
            call.latency_ms = round((time.perf_counter() - start) * 1000, 1)
            linker.finish(call)

    async def background(call: Call) -> None:
        async with semaphore:
            await execute(call, bounded=True)

    async def screen_lane(lane: list[Call]) -> None:
        failed = None
        for call in lane:
            if failed is not None:
                call.status = "skipped"
                call.error = f"skipped: earlier screen call #{failed.index} ({failed.tool}) failed"
                linker.finish(call)
                continue
            await execute(call, bounded=False)
            if call.status != "ok" and stop_screen_on_error:
                failed = call

    screen = [c for c in calls if c.lane == "screen"]
    jobs = [asyncio.ensure_future(background(c)) for c in calls if c.lane == "background"]
    if screen:
        jobs.append(asyncio.ensure_future(screen_lane(screen)))
    try:
        await asyncio.gather(*jobs)
    except asyncio.CancelledError:
        for job in jobs:
            job.cancel()
        raise
    wall = round((time.perf_counter() - t0) * 1000, 1)
    total = round(sum(c.latency_ms or 0 for c in calls), 1)
    return {
        "wall_ms": wall,
        "sum_ms": total,
        "speedup": round(total / wall, 2) if wall > 0 else None,
        "max_concurrency": max_concurrency,
        "ok": sum(c.status == "ok" for c in calls),
        "failed": sum(c.status != "ok" for c in calls),
        "results": [c.to_dict() for c in calls],
    }


def format_text(summary: dict) -> str:
    lines = [
        f"RunParallel: {summary['ok']}/{len(summary['results'])} ok, wall {summary['wall_ms']} ms "
        f"(sum of calls {summary['sum_ms']} ms, x{summary['speedup']})"
    ]
    for r in summary["results"]:
        link = f" task={r['plan_id']}/{r['task_id']}" if r["task_id"] else ""
        head = (
            f"[{r['index']}] {r['tool']} ({r['lane']}, {r['latency_ms']} ms){link}: {r['status']}"
        )
        body = r["result"] if r["ok"] else r["error"]
        lines.append(head + (f"\n{body}" if body else ""))
    return "\n".join(lines)


__all__ = ["Call", "parse_calls", "run_parallel", "format_text"]
