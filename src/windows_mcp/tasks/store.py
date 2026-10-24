"""In-memory task plans with DAG validation, a status state machine and optional JSON persistence.

The store is the single source of truth for the ``Tasks`` and ``RunParallel`` tools. Every
mutation happens under one re-entrant lock, is persisted (when a state file is configured) and
publishes events in mutation order through the attached ``TaskEventBus``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATUSES = ("pending", "running", "done", "failed", "skipped", "cancelled")
TERMINAL = frozenset({"done", "skipped", "cancelled"})
SATISFIED = frozenset({"done", "skipped"})  # A dependency in one of these unblocks dependents.
BLOCKING = frozenset({"failed", "cancelled"})  # A dependency in one of these blocks dependents.

# Allowed transitions. Same-status updates (progress only) are allowed for non-terminal states.
TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "done", "failed", "skipped", "cancelled"}),
    "running": frozenset({"pending", "done", "failed", "cancelled"}),
    "failed": frozenset({"pending", "running", "done", "skipped", "cancelled"}),  # retry/resolve
    "done": frozenset(),
    "skipped": frozenset(),
    "cancelled": frozenset(),
}

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_TEXT = 8000  # Stored result/error/progress size cap (characters).
MAX_TASKS = 200


class TaskError(ValueError):
    """Invalid plan input or an illegal state transition."""


def now_ms() -> int:
    return int(time.time() * 1000)


def _clip(value: Any, limit: int = MAX_TEXT) -> Any:
    """Keep JSON values as-is when small; stringify and truncate oversized ones."""
    if value is None:
        return None
    if isinstance(value, str):
        return (
            value
            if len(value) <= limit
            else value[:limit] + f"... [truncated {len(value) - limit} chars]"
        )
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    if len(text) <= limit:
        return json.loads(text)
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


@dataclass
class Task:
    id: str
    title: str
    app: str | None = None
    steps: Any = None
    needs_screen: bool = False
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    progress: str | None = None
    result: Any = None
    error: str | None = None
    attempts: int = 0
    created_at: int = field(default_factory=now_ms)
    updated_at: int = field(default_factory=now_ms)
    started_at: int | None = None
    finished_at: int | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "app": self.app,
            "steps": self.steps,
            "needs_screen": self.needs_screen,
            "depends_on": list(self.depends_on),
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


@dataclass
class Plan:
    id: str
    title: str | None = None
    tasks: dict[str, Task] = field(default_factory=dict)
    created_at: int = field(default_factory=now_ms)
    updated_at: int = field(default_factory=now_ms)

    def status(self) -> str:
        """Aggregate status: pending, active, blocked, done, failed or cancelled."""
        statuses = [t.status for t in self.tasks.values()]
        if not statuses:
            return "done"
        if any(s == "running" for s in statuses):
            return "active"
        if all(s in SATISFIED for s in statuses):
            return "done"
        pending = [t for t in self.tasks.values() if t.status == "pending"]
        if not pending:
            if any(s == "failed" for s in statuses):
                return "failed"
            return "cancelled"  # Only satisfied and cancelled tasks remain.
        if not any(self.is_ready(t) for t in pending):
            return "failed" if any(s == "failed" for s in statuses) else "blocked"
        return "pending" if all(s == "pending" for s in statuses) else "active"

    def is_ready(self, task: Task) -> bool:
        return task.status == "pending" and all(
            self.tasks[d].status in SATISFIED for d in task.depends_on
        )

    def is_blocked(self, task: Task) -> bool:
        """A pending task that can never become ready without intervention."""
        if task.status != "pending":
            return False
        seen: set[str] = set()
        stack = list(task.depends_on)
        while stack:
            dep = self.tasks[stack.pop()]
            if dep.id in seen:
                continue
            seen.add(dep.id)
            if dep.status in BLOCKING:
                return True
            if dep.status == "pending":
                stack.extend(dep.depends_on)
        return False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tasks": [t.to_dict() for t in self.tasks.values()],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        plan = cls(
            id=data["id"],
            title=data.get("title"),
            created_at=data.get("created_at", now_ms()),
            updated_at=data.get("updated_at", now_ms()),
        )
        for raw in data.get("tasks", []):
            task = Task.from_dict(raw)
            plan.tasks[task.id] = task
        return plan


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off", ""):
            return False
    raise TaskError(f"{name} must be true or false, got {value!r}")


def _as_id_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TaskError(f"{name} must be a list of task ids")
    return list(dict.fromkeys(value))  # Deduplicate, keep order.


def find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """Return one dependency cycle as a list of ids (first id repeated at the end), or None."""
    white, grey, black = 0, 1, 2
    color = {node: white for node in edges}
    parent: dict[str, str] = {}
    for root in edges:
        if color[root] != white:
            continue
        stack = [(root, iter(edges[root]))]
        color[root] = grey
        while stack:
            node, children = stack[-1]
            nxt = next(children, None)
            if nxt is None:
                color[node] = black
                stack.pop()
                continue
            if color.get(nxt, black) == grey:
                cycle = [nxt]
                cur = node
                while cur != nxt:
                    cycle.append(cur)
                    cur = parent[cur]
                cycle.append(nxt)
                return list(reversed(cycle))
            if color.get(nxt) == white:
                parent[nxt] = node
                color[nxt] = grey
                stack.append((nxt, iter(edges[nxt])))
    return None


class TaskStore:
    """Thread-safe plans keyed by id, with optional persistence and an event bus."""

    def __init__(self, *, path: str | Path | None = None, bus: Any = None) -> None:
        self._lock = threading.RLock()
        self._plans: dict[str, Plan] = {}
        self._counter = 0
        self.path = Path(path) if path else None
        self.bus = bus
        if self.path is not None:
            self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        assert self.path is not None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except OSError, ValueError:
            logger.warning("Ignoring unreadable task state file %s", self.path, exc_info=True)
            return
        self._counter = int(raw.get("counter", 0))
        for data in raw.get("plans", []):
            try:
                plan = Plan.from_dict(data)
            except KeyError, TypeError:
                logger.warning("Skipping malformed persisted plan", exc_info=True)
                continue
            for task in plan.tasks.values():
                if task.status == "running":
                    # The worker that owned this task died with the previous process.
                    task.status = "failed"
                    task.error = "interrupted: the server restarted while this task was running"
                    task.finished_at = task.updated_at = now_ms()
            self._plans[plan.id] = plan

    def _save(self) -> None:
        if self.path is None:
            return
        data = {
            "version": 1,
            "counter": self._counter,
            "plans": [p.to_dict() for p in self._plans.values()],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            logger.warning("Could not persist task state to %s", self.path, exc_info=True)

    # ------------------------------------------------------------------ events
    def _emit(self, event: dict) -> None:
        if self.bus is not None:
            try:
                self.bus.publish(event)
            except Exception:
                logger.debug("Task event publish failed", exc_info=True)

    def _task_event(self, plan: Plan, task: Task, prev: str | None) -> None:
        self._emit(
            {
                "type": "task_event",
                "plan_id": plan.id,
                "task_id": task.id,
                "title": task.title,
                "status": task.status,
                "prev_status": prev,
                "progress": task.progress,
                "result": task.result,
                "error": task.error,
                "needs_screen": task.needs_screen,
                "plan_status": plan.status(),
            }
        )

    def _plan_event(self, plan: Plan, event: str) -> None:
        self._emit(
            {
                "type": "plan_event",
                "event": event,
                "plan_id": plan.id,
                "title": plan.title,
                "plan_status": plan.status(),
                "plan": plan.to_dict(),
            }
        )

    # ------------------------------------------------------------------ building
    def _build_tasks(self, raw_tasks: Any, existing: dict[str, Task]) -> list[Task]:
        if isinstance(raw_tasks, str):
            try:
                raw_tasks = json.loads(raw_tasks)
            except ValueError as exc:
                raise TaskError(f"tasks must be a JSON list: {exc}") from exc
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise TaskError("tasks must be a non-empty list of task objects")
        if len(existing) + len(raw_tasks) > MAX_TASKS:
            raise TaskError(f"a plan may hold at most {MAX_TASKS} tasks")
        built: list[Task] = []
        ids = set(existing)
        auto = len(existing)
        for index, raw in enumerate(raw_tasks):
            if isinstance(raw, str):
                raw = {"title": raw}
            if not isinstance(raw, dict):
                raise TaskError(f"tasks[{index}] must be an object or a title string")
            unknown = set(raw) - {"id", "title", "app", "steps", "needs_screen", "depends_on"}
            if unknown:
                raise TaskError(f"tasks[{index}] has unknown fields: {sorted(unknown)}")
            auto += 1
            task_id = raw.get("id")
            if task_id is None:
                task_id = f"t{auto}"
                while task_id in ids:
                    auto += 1
                    task_id = f"t{auto}"
            if not isinstance(task_id, str) or not _ID_RE.match(task_id):
                raise TaskError(f"tasks[{index}].id must match {_ID_RE.pattern}, got {task_id!r}")
            if task_id in ids:
                raise TaskError(f"duplicate task id {task_id!r}")
            ids.add(task_id)
            title = raw.get("title")
            if not isinstance(title, str) or not title.strip():
                raise TaskError(f"tasks[{index}] ({task_id}) needs a non-empty title")
            app = raw.get("app")
            if app is not None and not isinstance(app, str):
                raise TaskError(f"tasks[{index}].app must be a string")
            steps = raw.get("steps")
            if steps is not None and not isinstance(steps, (str, list)):
                raise TaskError(f"tasks[{index}].steps must be a string or a list")
            built.append(
                Task(
                    id=task_id,
                    title=title.strip(),
                    app=app,
                    steps=_clip(steps),
                    needs_screen=_as_bool(raw.get("needs_screen", False), "needs_screen"),
                    depends_on=_as_id_list(raw.get("depends_on"), f"tasks[{index}].depends_on"),
                )
            )
        for task in built:
            for dep in task.depends_on:
                if dep == task.id:
                    raise TaskError(f"task {task.id!r} depends on itself")
                if dep not in ids:
                    raise TaskError(f"task {task.id!r} depends on unknown task {dep!r}")
        edges = {tid: list(t.depends_on) for tid, t in existing.items()}
        edges.update({t.id: list(t.depends_on) for t in built})
        cycle = find_cycle(edges)
        if cycle:
            raise TaskError("dependency cycle: " + " -> ".join(cycle))
        return built

    # ------------------------------------------------------------------ public API
    def create_plan(
        self, tasks: Any, *, title: str | None = None, plan_id: str | None = None
    ) -> Plan:
        with self._lock:
            if plan_id is not None:
                if not isinstance(plan_id, str) or not _ID_RE.match(plan_id):
                    raise TaskError(f"plan_id must match {_ID_RE.pattern}")
                if plan_id in self._plans:
                    raise TaskError(f"plan {plan_id!r} already exists")
            built = self._build_tasks(tasks, {})
            if plan_id is None:
                self._counter += 1
                plan_id = f"p{self._counter}"
                while plan_id in self._plans:
                    self._counter += 1
                    plan_id = f"p{self._counter}"
            plan = Plan(id=plan_id, title=title.strip() if isinstance(title, str) else None)
            for task in built:
                plan.tasks[task.id] = task
            self._plans[plan.id] = plan
            self._save()
            self._plan_event(plan, "created")
            for task in built:
                self._task_event(plan, task, None)
            return plan

    def add_tasks(self, plan_id: str | None, tasks: Any) -> list[Task]:
        with self._lock:
            plan = self.get_plan(plan_id)
            built = self._build_tasks(tasks, plan.tasks)
            for task in built:
                plan.tasks[task.id] = task
            plan.updated_at = now_ms()
            self._save()
            self._plan_event(plan, "tasks_added")
            for task in built:
                self._task_event(plan, task, None)
            return built

    def plans(self) -> list[Plan]:
        with self._lock:
            return list(self._plans.values())

    def get_plan(self, plan_id: str | None = None) -> Plan:
        """Return a plan by id; with no id, the most recently created plan."""
        with self._lock:
            if plan_id is None:
                if not self._plans:
                    raise TaskError("no plans exist; create one with mode='plan'")
                return next(reversed(self._plans.values()))
            plan = self._plans.get(plan_id)
            if plan is None:
                raise TaskError(f"unknown plan {plan_id!r}; known: {sorted(self._plans)}")
            return plan

    def resolve(self, plan_id: str | None, task_id: str) -> tuple[Plan, Task]:
        """Find a task; without a plan id, search the newest plan first."""
        with self._lock:
            if plan_id is not None:
                plan = self.get_plan(plan_id)
                task = plan.tasks.get(task_id)
                if task is None:
                    raise TaskError(f"plan {plan.id!r} has no task {task_id!r}")
                return plan, task
            for plan in reversed(self._plans.values()):
                if task_id in plan.tasks:
                    return plan, plan.tasks[task_id]
            raise TaskError(f"no plan has a task {task_id!r}")

    def snapshot_task(self, plan_id: str | None, task_id: str) -> dict:
        with self._lock:
            plan, task = self.resolve(plan_id, task_id)
            return {"plan_id": plan.id, **task.to_dict()}

    def update(
        self,
        plan_id: str | None,
        task_id: str,
        *,
        status: str | None = None,
        progress: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> Task:
        with self._lock:
            plan, task = self.resolve(plan_id, task_id)
            prev = task.status
            if status is not None and status not in STATUSES:
                raise TaskError(f"status must be one of {STATUSES}, got {status!r}")
            target = status or prev
            if target != prev and target not in TRANSITIONS[prev]:
                allowed = sorted(TRANSITIONS[prev]) or "none (terminal)"
                raise TaskError(
                    f"task {task.id!r} cannot go from {prev!r} to {target!r}; allowed: {allowed}"
                )
            if target == prev and prev in TERMINAL:
                raise TaskError(f"task {task.id!r} is already {prev!r} (terminal)")
            stamp = now_ms()
            if target == "running" and prev != "running":
                task.attempts += 1
                task.started_at = stamp
                task.finished_at = None
                if prev == "failed":
                    task.error = None
            if target == "pending" and prev != "pending":
                task.started_at = task.finished_at = None
            if target in TERMINAL or target == "failed":
                task.finished_at = stamp
            task.status = target
            if progress is not None:
                task.progress = _clip(str(progress), 1000)
            if result is not None:
                task.result = _clip(result)
            if error is not None:
                task.error = _clip(str(error))
            task.updated_at = plan.updated_at = stamp
            self._save()
            self._task_event(plan, task, prev)
            return task

    def cancel(self, plan_id: str | None, task_id: str | None = None) -> list[Task]:
        """Cancel one task, or every non-terminal task of a plan. Returns cancelled tasks."""
        with self._lock:
            plan = self.get_plan(plan_id)
            targets = [plan.tasks[task_id]] if task_id else list(plan.tasks.values())
            if task_id and task_id not in plan.tasks:
                raise TaskError(f"plan {plan.id!r} has no task {task_id!r}")
            changed = []
            for task in targets:
                if task.status in TERMINAL:
                    continue
                prev = task.status
                task.status = "cancelled"
                task.finished_at = task.updated_at = plan.updated_at = now_ms()
                changed.append((task, prev))
            if changed:
                self._save()
                for task, prev in changed:
                    self._task_event(plan, task, prev)
                if task_id is None:
                    self._plan_event(plan, "cancelled")
            return [t for t, _ in changed]

    def clear(self, plan_id: str | None = None) -> list[str]:
        """Remove one plan, or all plans when *plan_id* is None. Returns removed ids."""
        with self._lock:
            if plan_id is not None:
                plan = self.get_plan(plan_id)
                removed = [self._plans.pop(plan.id)]
            else:
                removed = list(self._plans.values())
                self._plans.clear()
            self._save()
            for plan in removed:
                self._plan_event(plan, "cleared")
            return [p.id for p in removed]

    def next_ready(self, plan_id: str | None = None) -> dict:
        """Split ready tasks into a sequential screen lane and a parallel background lane."""
        with self._lock:
            plan = self.get_plan(plan_id)
            running = [t for t in plan.tasks.values() if t.status == "running"]
            screen_busy = [t.id for t in running if t.needs_screen]
            ready = [t for t in plan.tasks.values() if plan.is_ready(t)]
            blocked = [t for t in plan.tasks.values() if plan.is_blocked(t)]
            waiting = [
                t
                for t in plan.tasks.values()
                if t.status == "pending" and not plan.is_ready(t) and not plan.is_blocked(t)
            ]
            return {
                "plan_id": plan.id,
                "plan_status": plan.status(),
                "screen": [t.to_dict() for t in ready if t.needs_screen],
                "background": [t.to_dict() for t in ready if not t.needs_screen],
                "screen_busy": screen_busy,
                "running": [t.id for t in running],
                "waiting": [t.id for t in waiting],
                "blocked": [t.id for t in blocked],
                "failed": [t.id for t in plan.tasks.values() if t.status == "failed"],
            }
