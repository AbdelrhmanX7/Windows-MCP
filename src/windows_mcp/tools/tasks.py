"""Tasks and RunParallel tools: plan dependent work, track it live, and run calls concurrently."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations

from windows_mcp.infrastructure import with_analytics
from windows_mcp.tasks.parallel import format_text, parse_calls, run_parallel
from windows_mcp.tasks.runtime import OrchestrationMiddleware, TaskRuntime, get_runtime
from windows_mcp.tasks.store import Plan, TaskError, _as_bool

TASKS_DESCRIPTION = (
    "Track a multi-step request as a plan of dependent tasks; every change is pushed live "
    "to the UI as a task_event. Modes: "
    "'plan' (create: title + tasks=[{id?, title, app?, steps?, needs_screen?, depends_on?}]; "
    "ids default to t1, t2, ...; cycles and unknown deps are rejected), "
    "'add' (append tasks to plan_id), "
    "'update' (task_id + status pending|running|done|failed|skipped|cancelled, progress, "
    "result, error; or updates=[{task_id, status, ...}] for several at once), "
    "'next' (tasks ready now: a 'screen' lane to run one at a time in order and a "
    "'background' lane safe to run in parallel, e.g. via RunParallel), "
    "'list' (all plans, or one plan's tasks), 'get' (one plan or task), "
    "'cancel' (a task or a whole plan), 'clear' (remove a plan, or all plans), "
    "'events' (recent events after seq=since, for polling clients). "
    "plan_id defaults to the newest plan. format='json' returns machine-readable JSON. "
    "Workflow: plan -> next -> run (RunParallel with task_id links updates automatically) "
    "-> next ... until plan_status is done."
)

RUN_PARALLEL_DESCRIPTION = (
    "Run several tool calls in one turn: calls=[{tool, args, task_id?, plan_id?, lane?}]. "
    "Background calls (FileSystem, PowerShell, Registry, Process list, Clipboard get, "
    "Notification, Scrape, Screenshot, App launch, ...) run concurrently, bounded by "
    "max_concurrency (default 4). Screen calls (Click, Type, Scroll, Move, Shortcut, "
    "MultiSelect, MultiEdit, App switch/resize, and any call with lane='screen') run one at "
    "a time in the given order through the screen lock, alongside the background calls; "
    "after a failed screen call the remaining screen calls are skipped unless "
    "stop_screen_on_error=false. Put an App launch that must take focus before a Type in "
    "lane='screen'. Results come back in input order with per-call latency and error. A "
    "call with task_id marks that task running, then done (result) or failed (error). "
    "Every inner call passes the same control gate and policy as a direct call. "
    "timeout is per background call (seconds, default 60)."
)


def _json_arg(value: Any, name: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be JSON: {exc}") from exc
    return value


def _task_line(task: dict) -> str:
    bits = [f"  {task['id']} [{task['status']}] {task['title']}"]
    if task.get("app"):
        bits.append(f"app={task['app']}")
    if task.get("needs_screen"):
        bits.append("screen")
    if task.get("depends_on"):
        bits.append("after " + ",".join(task["depends_on"]))
    line = " | ".join(bits)
    if task.get("progress") and task["status"] not in ("done",):
        line += f"\n      progress: {task['progress']}"
    if task.get("error"):
        line += f"\n      error: {str(task['error'])[:300]}"
    elif task.get("result") is not None and task["status"] == "done":
        text = task["result"] if isinstance(task["result"], str) else json.dumps(task["result"])
        line += f"\n      result: {text[:200]}"
    return line


def _plan_text(plan: Plan) -> str:
    data = plan.to_dict()
    done = sum(t["status"] in ("done", "skipped") for t in data["tasks"])
    title = f' "{data["title"]}"' if data["title"] else ""
    lines = [f"plan {data['id']}{title} [{data['status']}] {done}/{len(data['tasks'])} done"]
    lines += [_task_line(t) for t in data["tasks"]]
    return "\n".join(lines)


def _next_text(ready: dict) -> str:
    screen = [t["id"] for t in ready["screen"]]
    background = [t["id"] for t in ready["background"]]
    lines = [f"next for plan {ready['plan_id']} [{ready['plan_status']}]"]
    lines.append(
        "  screen (run one at a time, in order): " + (", ".join(screen) if screen else "-")
    )
    if ready["screen_busy"]:
        lines.append("  screen lane busy with: " + ", ".join(ready["screen_busy"]))
    lines.append("  background (parallel): " + (", ".join(background) if background else "-"))
    for key in ("running", "waiting", "blocked", "failed"):
        if ready[key]:
            lines.append(f"  {key}: " + ", ".join(ready[key]))
    for t in ready["screen"] + ready["background"]:
        lines.append(_task_line(t))
    if ready["plan_status"] == "done":
        lines.append("  all tasks finished")
    return "\n".join(lines)


def register(mcp, *, get_desktop, get_analytics, runtime: TaskRuntime | None = None):
    """Register Tasks and RunParallel, and install the orchestration middleware."""

    def rt() -> TaskRuntime:
        return runtime or get_runtime()

    middleware = getattr(mcp, "middleware", None)
    if isinstance(middleware, list):
        # Outermost: the FIFO screen queue orders screen calls before the control gate runs.
        middleware.insert(0, OrchestrationMiddleware(rt()))

    @mcp.tool(
        name="Tasks",
        description=TASKS_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Tasks",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Tasks-Tool")
    def tasks_tool(
        mode: Literal["plan", "add", "update", "list", "get", "next", "cancel", "clear", "events"],
        plan_id: str | None = None,
        title: str | None = None,
        tasks: list[dict] | str | None = None,
        task_id: str | None = None,
        status: Literal["pending", "running", "done", "failed", "skipped", "cancelled"]
        | None = None,
        progress: str | None = None,
        result: str | dict | list | int | float | bool | None = None,
        error: str | None = None,
        updates: list[dict] | str | None = None,
        since: int = 0,
        format: Literal["text", "json"] = "text",
        ctx: Context = None,
    ) -> str:
        store = rt().store
        as_json = format == "json"

        def out(data: Any, text: str) -> str:
            return json.dumps(data, ensure_ascii=False, default=str) if as_json else text

        try:
            if mode == "plan":
                if tasks is None:
                    raise TaskError("mode='plan' needs tasks=[...]")
                plan = store.create_plan(tasks, title=title, plan_id=plan_id)
                ready = store.next_ready(plan.id)
                return out(
                    {"plan": plan.to_dict(), "next": ready},
                    _plan_text(plan) + "\n" + _next_text(ready),
                )
            if mode == "add":
                if tasks is None:
                    raise TaskError("mode='add' needs tasks=[...]")
                store.add_tasks(plan_id, tasks)
                plan = store.get_plan(plan_id)
                return out(plan.to_dict(), _plan_text(plan))
            if mode == "update":
                batch = _json_arg(updates, "updates")
                if batch is None:
                    if task_id is None:
                        raise TaskError("mode='update' needs task_id (or updates=[...])")
                    batch = [
                        {
                            "task_id": task_id,
                            "status": status,
                            "progress": progress,
                            "result": result,
                            "error": error,
                        }
                    ]
                if not isinstance(batch, list) or not batch:
                    raise TaskError("updates must be a non-empty list")
                changed = []
                for index, item in enumerate(batch):
                    if not isinstance(item, dict) or not isinstance(item.get("task_id"), str):
                        raise TaskError(f"updates[{index}] needs a task_id")
                    extra = set(item) - {
                        "task_id",
                        "plan_id",
                        "status",
                        "progress",
                        "result",
                        "error",
                    }
                    if extra:
                        raise TaskError(f"updates[{index}] has unknown fields: {sorted(extra)}")
                    task = store.update(
                        item.get("plan_id", plan_id),
                        item["task_id"],
                        status=item.get("status"),
                        progress=item.get("progress"),
                        result=item.get("result"),
                        error=item.get("error"),
                    )
                    changed.append(task.to_dict())
                lines = [f"{t['id']} -> {t['status']}" for t in changed]
                return out({"updated": changed}, "updated: " + "; ".join(lines))
            if mode == "list":
                if plan_id is not None:
                    plan = store.get_plan(plan_id)
                    return out(plan.to_dict(), _plan_text(plan))
                plans = store.plans()
                if not plans:
                    return out({"plans": []}, "no plans")
                return out(
                    {"plans": [p.to_dict() for p in plans]},
                    "\n".join(_plan_text(p) for p in plans),
                )
            if mode == "get":
                if task_id is not None:
                    snap = store.snapshot_task(plan_id, task_id)
                    return out(snap, f"plan {snap['plan_id']}\n" + _task_line(snap))
                plan = store.get_plan(plan_id)
                return out(plan.to_dict(), _plan_text(plan))
            if mode == "next":
                ready = store.next_ready(plan_id)
                return out(ready, _next_text(ready))
            if mode == "cancel":
                cancelled = store.cancel(plan_id, task_id)
                ids = [t.id for t in cancelled]
                return out(
                    {"cancelled": ids},
                    "cancelled: " + (", ".join(ids) if ids else "nothing (already finished)"),
                )
            if mode == "clear":
                removed = store.clear(plan_id)
                return out({"cleared": removed}, "cleared: " + (", ".join(removed) or "-"))
            if mode == "events":
                events = rt().bus.history(since=since, plan_id=plan_id)
                payload = {"events": events, "last_seq": rt().bus.last_seq}
                return json.dumps(payload, ensure_ascii=False, default=str)
            raise TaskError(f"unknown mode {mode!r}")
        except TaskError as exc:
            raise ValueError(str(exc)) from None

    @mcp.tool(
        name="RunParallel",
        description=RUN_PARALLEL_DESCRIPTION,
        annotations=ToolAnnotations(
            title="RunParallel",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @with_analytics(get_analytics(), "Run-Parallel-Tool")
    async def run_parallel_tool(
        calls: list[dict] | str,
        plan_id: str | None = None,
        max_concurrency: int | None = None,
        timeout: float | None = None,
        stop_screen_on_error: bool | str = True,
        max_result_chars: int = 4000,
        format: Literal["text", "json"] = "text",
        ctx: Context = None,
    ) -> str:
        stop = _as_bool(stop_screen_on_error, "stop_screen_on_error")
        parsed = parse_calls(calls, default_plan=plan_id)
        try:
            summary = await run_parallel(
                lambda name, args: mcp.call_tool(name, args),
                parsed,
                store=rt().store,
                max_concurrency=max_concurrency,
                timeout=timeout,
                stop_screen_on_error=stop,
                max_result_chars=max(200, min(max_result_chars, 50000)),
                screen_lock=rt().screen_lock,
            )
        except TaskError as exc:
            raise ValueError(str(exc)) from None
        if format == "json":
            return json.dumps(summary, ensure_ascii=False, default=str)
        return format_text(summary)
