"""Tasks + RunParallel over an in-process FastMCP client: lanes, lock, events, policy."""

from __future__ import annotations

import asyncio
import inspect
import json
import time

import pytest
from fastmcp import Client, FastMCP

from windows_mcp.tasks.runtime import TaskRuntime
from windows_mcp.tasks.screen_lock import ScreenLock
from windows_mcp.tools import tasks as tasks_module


def _legacy_client(transport, **kwargs):
    """Unsolicited (out-of-request) logs need the sessionful protocol era on FastMCP 4."""
    if "mode" in inspect.signature(Client).parameters:
        kwargs["mode"] = "legacy"
    return Client(transport, **kwargs)


class Probe:
    """Fake tools that record when they run, so overlap and order can be asserted."""

    def __init__(self):
        self.spans: list[tuple[str, float, float]] = []
        self.active = {"screen": 0, "any": 0}
        self.peak = {"screen": 0, "any": 0}

    def install(self, mcp, names, *, screen: bool, delay: float = 0.2):
        for name in names:
            mcp.tool(name=name)(self._make(name, screen, delay))

    def _make(self, name, screen, default_delay):
        async def tool(
            label: str = "", delay: float = default_delay, fail: bool = False, mode: str = "x"
        ) -> str:
            start = time.perf_counter()
            self.active["any"] += 1
            self.peak["any"] = max(self.peak["any"], self.active["any"])
            if screen:
                self.active["screen"] += 1
                self.peak["screen"] = max(self.peak["screen"], self.active["screen"])
            try:
                await asyncio.sleep(delay)
                if fail:
                    raise RuntimeError(f"{name} failed")
                return f"{name}:{label}:{'screen' if screen else 'bg'}"
            finally:
                self.active["any"] -= 1
                if screen:
                    self.active["screen"] -= 1
                self.spans.append((f"{name}:{label}", start, time.perf_counter()))

        return tool


def build(tmp_path=None, *, gate=None, lock=None, **runtime_kwargs):
    mcp = FastMCP("tasks-test")
    if gate is not None:
        mcp.add_middleware(gate)  # Same order as __main__: gate first, then register_all.
    runtime = TaskRuntime.create(screen_lock=lock or ScreenLock(), **runtime_kwargs)
    probe = Probe()
    probe.install(mcp, ["PowerShell", "FileSystem", "Registry", "Wait"], screen=False)
    probe.install(mcp, ["Click", "Type", "Shortcut"], screen=True)
    tasks_module.register(
        mcp, get_desktop=lambda: None, get_analytics=lambda: None, runtime=runtime
    )
    return mcp, runtime, probe


async def call(client, name, args, **kw):
    result = await client.call_tool(name, args, raise_on_error=False, **kw)
    return result, (result.content[0].text if result.content else "")


@pytest.mark.asyncio
async def test_tasks_tool_flow_json_and_text():
    mcp, runtime, _ = build()
    async with Client(mcp) as client:
        _, text = await call(
            client,
            "Tasks",
            {
                "mode": "plan",
                "title": "Groceries + music",
                "tasks": [
                    {"id": "note", "title": "Write note", "app": "Notepad", "needs_screen": True},
                    {"id": "music", "title": "Play music", "app": "Spotify"},
                    {"id": "tell", "title": "Notify", "depends_on": ["note", "music"]},
                ],
            },
        )
        assert 'plan p1 "Groceries + music" [pending] 0/3 done' in text
        assert "screen (run one at a time, in order): note" in text
        assert "background (parallel): music" in text

        _, raw = await call(client, "Tasks", {"mode": "next", "format": "json"})
        ready = json.loads(raw)
        assert [t["id"] for t in ready["screen"]] == ["note"]
        assert [t["id"] for t in ready["background"]] == ["music"]

        _, text = await call(
            client,
            "Tasks",
            {
                "mode": "update",
                "updates": [
                    {"task_id": "note", "status": "done", "result": "saved"},
                    {"task_id": "music", "status": "done"},
                ],
            },
        )
        assert text == "updated: note -> done; music -> done"
        _, raw = await call(client, "Tasks", {"mode": "get", "task_id": "tell", "format": "json"})
        assert json.loads(raw)["status"] == "pending"

        bad, text = await call(
            client, "Tasks", {"mode": "update", "task_id": "note", "status": "running"}
        )
        assert bad.is_error and "cannot go from 'done'" in text
        bad, text = await call(
            client,
            "Tasks",
            {"mode": "plan", "tasks": [{"id": "a", "title": "A", "depends_on": ["a"]}]},
        )
        assert bad.is_error and "depends on itself" in text

        _, raw = await call(client, "Tasks", {"mode": "events", "since": 0})
        events = json.loads(raw)["events"]
        assert events[0]["type"] == "plan_event" and events[-1]["status"] == "done"

        _, text = await call(client, "Tasks", {"mode": "cancel"})
        assert text == "cancelled: tell"
        _, text = await call(client, "Tasks", {"mode": "clear"})
        assert text == "cleared: p1"


@pytest.mark.asyncio
async def test_events_reach_mcp_client_as_notifications(tmp_path):
    hook = []
    mcp, runtime, _ = build(
        webhook_url="https://example.invalid/hook",
        poster=lambda url, **kw: hook.append(kw["json"]),
        jsonl_path=tmp_path / "events.jsonl",
    )
    received = []

    async def on_log(message):
        if message.logger == "windows-mcp.tasks":
            received.append(message.data)

    async with _legacy_client(mcp, log_handler=on_log) as client:
        await client.call_tool("Tasks", {"mode": "list"})  # Registers the live session.
        # A change made outside any request (e.g. a backend thread) still reaches the client.
        await asyncio.to_thread(
            runtime.store.create_plan, [{"id": "a", "title": "A"}], title="unsolicited"
        )
        for _ in range(100):
            if len(received) >= 2:
                break
            await asyncio.sleep(0.02)
    assert [e["type"] for e in received] == ["plan_event", "task_event"]
    task_event = received[1]
    for key in ("type", "plan_id", "task_id", "status", "progress", "result", "error", "ts", "seq"):
        assert key in task_event
    assert task_event["status"] == "pending" and task_event["plan_id"] == "p1"
    assert runtime.bus.flush_webhook(5)
    assert [e["seq"] for e in hook] == [e["seq"] for e in received]
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["seq"] for line in lines] == [1, 2]


@pytest.mark.asyncio
async def test_run_parallel_overlaps_background_and_serializes_screen():
    mcp, runtime, probe = build()
    calls = [
        {"tool": "PowerShell", "args": {"label": "a", "delay": 0.3}},
        {"tool": "Click", "args": {"label": "c1", "delay": 0.1}},
        {"tool": "FileSystem", "args": {"label": "b", "delay": 0.3}},
        {"tool": "Type", "args": {"label": "t1", "delay": 0.1}},
        {"tool": "Registry", "args": {"label": "c", "delay": 0.3}},
        {"tool": "Shortcut", "args": {"label": "s1", "delay": 0.1}},
        {"tool": "Wait", "args": {"label": "d", "delay": 0.3}},
    ]
    async with Client(mcp) as client:
        t0 = time.perf_counter()
        _, raw = await call(client, "RunParallel", {"calls": calls, "format": "json"})
        wall = time.perf_counter() - t0
    summary = json.loads(raw)
    results = summary["results"]
    # Order of results matches input order, each with its own latency.
    assert [r["index"] for r in results] == list(range(7))
    assert [r["result"].split(":")[0] for r in results] == [c["tool"] for c in calls]
    assert all(r["ok"] and r["latency_ms"] > 0 for r in results)
    assert [r["lane"] for r in results] == [
        "background",
        "screen",
        "background",
        "screen",
        "background",
        "screen",
        "background",
    ]
    # Four 0.3 s background calls in parallel plus a 0.3 s screen chain: well under 1.5 s.
    assert wall < 1.0, wall
    assert probe.peak["any"] >= 4
    assert probe.peak["screen"] == 1
    screen_spans = sorted(
        (s for s in probe.spans if s[0][:2] in ("Cl", "Ty", "Sh")), key=lambda s: s[1]
    )
    assert [s[0] for s in screen_spans] == ["Click:c1", "Type:t1", "Shortcut:s1"]
    for prev, nxt in zip(screen_spans, screen_spans[1:]):
        assert prev[2] <= nxt[1]  # Never interleaved.
    assert summary["speedup"] > 1.5


@pytest.mark.asyncio
async def test_run_parallel_bounded_concurrency_timeout_and_errors():
    mcp, _, probe = build()
    calls = [{"tool": "PowerShell", "args": {"label": str(i), "delay": 0.15}} for i in range(4)]
    calls += [
        {"tool": "FileSystem", "args": {"label": "slow", "delay": 2}},
        {"tool": "NoSuchTool", "args": {}},
        {"tool": "Registry", "args": {"label": "x", "fail": True, "delay": 0}},
    ]
    async with Client(mcp) as client:
        _, raw = await call(
            client,
            "RunParallel",
            {"calls": calls, "max_concurrency": 2, "timeout": 0.5, "format": "json"},
        )
    results = json.loads(raw)["results"]
    assert probe.peak["any"] <= 2
    assert results[4]["status"] == "timeout" and "0.5" in results[4]["error"]
    assert results[5]["status"] == "error" and "NoSuchTool" in results[5]["error"]
    assert results[6]["status"] == "error" and "Registry failed" in results[6]["error"]
    assert all(r["ok"] for r in results[:4])


@pytest.mark.asyncio
async def test_screen_failure_skips_rest_of_screen_lane_and_links_tasks():
    mcp, runtime, _ = build()
    async with Client(mcp) as client:
        await call(
            client,
            "Tasks",
            {
                "mode": "plan",
                "tasks": [
                    {"id": "bg", "title": "background"},
                    {"id": "ui", "title": "screen work", "needs_screen": True},
                    {"id": "after", "title": "after", "needs_screen": True},
                ],
            },
        )
        _, raw = await call(
            client,
            "RunParallel",
            {
                "format": "json",
                "calls": [
                    {"tool": "PowerShell", "args": {"label": "1", "delay": 0.05}, "task_id": "bg"},
                    {"tool": "FileSystem", "args": {"label": "2", "delay": 0.05}, "task_id": "bg"},
                    {"tool": "Click", "args": {"fail": True, "delay": 0}, "task_id": "ui"},
                    {"tool": "Type", "args": {"label": "never"}, "task_id": "after"},
                ],
            },
        )
        results = json.loads(raw)["results"]
        _, raw = await call(client, "Tasks", {"mode": "list", "plan_id": "p1", "format": "json"})
    tasks = {t["id"]: t for t in json.loads(raw)["tasks"]}
    assert results[3]["status"] == "skipped"
    assert tasks["bg"]["status"] == "done"
    assert tasks["bg"]["result"] == ["PowerShell:1:bg", "FileSystem:2:bg"]
    assert tasks["ui"]["status"] == "failed" and "Click failed" in tasks["ui"]["error"]
    assert tasks["after"]["status"] == "failed" and "skipped" in tasks["after"]["error"]
    events = [e for e in runtime.bus.history() if e.get("task_id") == "bg"]
    assert [e["status"] for e in events] == ["pending", "running", "running", "done"]


@pytest.mark.asyncio
async def test_run_parallel_rejects_bad_input_and_unknown_task():
    mcp, _, _ = build()
    async with Client(mcp) as client:
        for args, needle in (
            ({"calls": []}, "non-empty"),
            ({"calls": [{"tool": "RunParallel"}]}, "cannot be nested"),
            ({"calls": [{"tool": "Click", "lane": "background"}]}, None),
            ({"calls": [{"tool": "Click", "lane": "sideways"}]}, "lane must be"),
            ({"calls": [{"tool": "PowerShell", "task_id": "ghost"}]}, "no plan"),
        ):
            result, text = await call(client, "RunParallel", args)
            if needle is None:
                assert not result.is_error and "(screen," in text  # Opting out is ignored.
            else:
                assert result.is_error and needle in text, text


@pytest.mark.asyncio
async def test_screen_lock_serializes_concurrent_mcp_requests_fifo():
    mcp, _, probe = build()
    async with Client(mcp) as client:
        jobs = []
        for i, name in enumerate(["Click", "Type", "Shortcut", "Click", "Type"]):
            jobs.append(
                asyncio.create_task(client.call_tool(name, {"label": str(i), "delay": 0.05}))
            )
            await asyncio.sleep(0.01)
        jobs.append(
            asyncio.create_task(client.call_tool("PowerShell", {"label": "bg", "delay": 0.05}))
        )
        await asyncio.wait_for(asyncio.gather(*jobs), 5)
    screen = sorted(
        (s for s in probe.spans if not s[0].startswith("PowerShell")), key=lambda s: s[1]
    )
    assert probe.peak["screen"] == 1
    assert [s[0].split(":")[1] for s in screen] == ["0", "1", "2", "3", "4"]  # FIFO.
    bg = next(s for s in probe.spans if s[0].startswith("PowerShell"))
    assert bg[1] < screen[-1][1]  # Background did not wait behind the screen queue.


@pytest.mark.asyncio
async def test_screen_lock_timeout_is_a_clear_tool_error(monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_SCREEN_LOCK_TIMEOUT", "0.1")
    mcp, _, _ = build()
    async with Client(mcp) as client:
        slow = asyncio.create_task(client.call_tool("Type", {"delay": 0.6}))
        await asyncio.sleep(0.05)
        result, text = await call(client, "Click", {"delay": 0})
        await slow
    assert result.is_error and "SCREEN_BUSY" in text and "held by Type" in text


@pytest.mark.asyncio
async def test_cancelled_screen_call_keeps_lock_until_tool_finishes():
    lock = ScreenLock()
    mcp, _, probe = build(lock=lock)
    async with Client(mcp) as client:
        job = asyncio.create_task(client.call_tool("Type", {"label": "long", "delay": 0.4}))
        await asyncio.sleep(0.1)
        assert lock.locked
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        await asyncio.sleep(0.05)
        # The request was cancelled, but the tool is still "typing": lock still held.
        assert lock.locked or any(s[0] == "Type:long" for s in probe.spans)
        for _ in range(100):
            if not lock.locked:
                break
            await asyncio.sleep(0.01)
        assert not lock.locked


# ---------------------------------------------------------------- control gate + policy


def _gate_parts():
    pytest.importorskip("win32api")  # The real coordinator needs Windows.
    from windows_mcp.desktop.control import ControlCoordinator
    from windows_mcp.tools.control_notifications import ControlNotifier, ControlToolGate

    return ControlCoordinator, ControlNotifier, ControlToolGate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,policy", [(False, "relaxed"), (True, "strict"), (True, "balanced")]
)
async def test_run_parallel_does_not_deadlock_in_gate(enabled, policy):
    ControlCoordinator, ControlNotifier, ControlToolGate = _gate_parts()
    from unittest.mock import Mock

    owner = ControlCoordinator()
    owner.enabled, owner.policy = enabled, policy
    owner.status = Mock(return_value={"state": "ready", "generation": 1})
    owner.begin_call = Mock(return_value=7)
    owner.checkpoint = Mock()
    owner.end_call = Mock()
    mcp, _, _ = build(gate=ControlToolGate(owner, ControlNotifier(owner)))
    async with Client(mcp) as client:
        result, text = await asyncio.wait_for(
            call(
                client,
                "RunParallel",
                {
                    "calls": [
                        {"tool": "PowerShell", "args": {"delay": 0.01}},
                        {"tool": "Click", "args": {"delay": 0.01}},
                        {"tool": "Tasks", "args": {"mode": "list"}},
                    ]
                },
            ),
            5,
        )
    assert not result.is_error and "3/3 ok" in text
    if enabled:
        # Each inner call is gated individually; the orchestrator never takes a lease.
        names = [c.args[0] for c in owner.begin_call.call_args_list]
        assert "RunParallel" not in names and "Tasks" not in names and "Click" in names
        assert ("PowerShell" in names) is (policy in ("strict", "balanced"))


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["relaxed", "balanced", "strict"])
async def test_run_parallel_policy_per_inner_call_during_user_takeover(policy):
    ControlCoordinator, ControlNotifier, ControlToolGate = _gate_parts()
    owner = ControlCoordinator()
    owner.policy = policy
    owner._state = "user"
    owner._last_user = time.monotonic()
    mcp, runtime, probe = build(gate=ControlToolGate(owner, ControlNotifier(owner)))
    async with Client(mcp) as client:
        # Whole batch would need ownership: rejected before anything runs.
        result, text = await call(
            client,
            "RunParallel",
            {"calls": [{"tool": "Wait", "args": {}}, {"tool": "Click", "args": {}}]},
        )
        assert result.is_error and "USER_CONTROL" in text and probe.spans == []
        # Tasks stays usable so the UI keeps updating during a takeover.
        result, _ = await call(client, "Tasks", {"mode": "list"})
        assert not result.is_error
        # Background-only batch: each call gets the same verdict it would get directly.
        result, raw = await call(
            client,
            "RunParallel",
            {
                "format": "json",
                "calls": [{"tool": "Wait", "args": {}}, {"tool": "PowerShell", "args": {}}],
            },
        )
    if policy == "relaxed":
        assert not result.is_error
        assert [r["ok"] for r in json.loads(raw)["results"]] == [True, True]
    else:
        assert result.is_error and "USER_CONTROL" in raw  # Strictest inner call needs control.


@pytest.mark.asyncio
async def test_events_ride_the_in_flight_request_on_modern_protocol():
    """Modern-era clients only accept logs on a request's own stream: RunParallel's."""
    mcp, runtime, _ = build()
    received = []

    async def on_log(message):
        if message.logger == "windows-mcp.tasks":
            received.append(message.data)

    async with Client(mcp, log_handler=on_log) as client:
        await client.call_tool("Tasks", {"mode": "plan", "tasks": [{"id": "a", "title": "A"}]})
        received.clear()
        await client.call_tool(
            "RunParallel",
            {"calls": [{"tool": "PowerShell", "args": {"delay": 0.2}, "task_id": "a"}]},
        )
        for _ in range(50):
            if any(e.get("status") == "done" for e in received):
                break
            await asyncio.sleep(0.02)
    statuses = [e["status"] for e in received if e["type"] == "task_event"]
    assert "running" in statuses and "done" in statuses


@pytest.mark.asyncio
async def test_all_events_of_a_tasks_call_arrive_on_modern_protocol():
    """A plan publishes several events from a worker thread; none may be lost at response time."""
    mcp, _, _ = build()
    received = []

    async def on_log(message):
        if message.logger == "windows-mcp.tasks":
            received.append(message.data)

    async with Client(mcp, log_handler=on_log) as client:
        for round_ in range(3):
            received.clear()
            await client.call_tool(
                "Tasks",
                {"mode": "plan", "tasks": [{"title": f"task {i}"} for i in range(4)]},
            )
            assert [e["type"] for e in received] == ["plan_event"] + ["task_event"] * 4, round_


@pytest.mark.asyncio
async def test_forced_screen_lane_call_holds_the_screen_lock():
    """A lane='screen' Wait/App launch keeps concurrent screen requests out until it ends."""
    lock = ScreenLock()
    mcp, _, probe = build(lock=lock)
    async with Client(mcp) as client:
        batch = asyncio.create_task(
            client.call_tool(
                "RunParallel",
                {
                    "calls": [
                        {"tool": "Wait", "args": {"label": "w", "delay": 0.3}, "lane": "screen"}
                    ]
                },
            )
        )
        await asyncio.sleep(0.1)
        assert lock.status()["holder"] == "Wait"
        await client.call_tool("Click", {"label": "outside", "delay": 0})
        await batch
    spans = {s[0]: s for s in probe.spans}
    assert spans["Click:outside"][1] >= spans["Wait:w"][2]  # Click waited for the Wait.
    assert not lock.locked
