"""Do and Route over an in-process FastMCP client: gate, screen lock re-entry, events, errors."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastmcp import Client, FastMCP

from windows_mcp.models import http as model_http
from windows_mcp.tasks.runtime import TaskRuntime
from windows_mcp.tasks.screen_lock import ScreenLock
from windows_mcp.tools import agent as agent_module
from windows_mcp.tools import tasks as tasks_module
from windows_mcp.tools.control_policy import (
    CLASSIFIED_TOOLS,
    category,
    is_screen_call,
    requires_control,
)

FAKE_OR = "sk-or-test-NOT-A-REAL-KEY-789"


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict, float, float]] = []

    def install(self, mcp):
        rec = self

        async def snapshot(
            use_vision: bool = True, use_annotation: bool = True, use_dom: bool = False
        ) -> str:
            rec.calls.append(("Snapshot", {"use_vision": use_vision}, time.perf_counter(), 0))
            return "Focused Window: Notepad\nInteractive Elements:\n0 Edit 'Text editor'"

        async def click(target: str | None = None, loc: list[int] | None = None) -> str:
            start = time.perf_counter()
            await asyncio.sleep(0.05)
            rec.calls.append(("Click", {"target": target}, start, time.perf_counter()))
            return f"Single left clicked {target}"

        async def type_(text: str, target: str | None = None) -> str:
            start = time.perf_counter()
            await asyncio.sleep(0.05)
            rec.calls.append(("Type", {"text": text, "target": target}, start, time.perf_counter()))
            return f"Typed {text}"

        async def app(name: str, mode: str = "launch") -> str:
            rec.calls.append(("App", {"name": name}, time.perf_counter(), 0))
            return f"Launched {name}"

        mcp.tool(name="Snapshot")(snapshot)
        mcp.tool(name="Click")(click)
        mcp.tool(name="Type")(type_)
        mcp.tool(name="App")(app)


def build(*, gate=None, lock=None):
    mcp = FastMCP("do-test")
    if gate is not None:
        mcp.add_middleware(gate)
    runtime = TaskRuntime.create(screen_lock=lock or ScreenLock())
    rec = Recorder()
    rec.install(mcp)
    tasks_module.register(
        mcp, get_desktop=lambda: None, get_analytics=lambda: None, runtime=runtime
    )
    agent_module.register(
        mcp, get_desktop=lambda: None, get_analytics=lambda: None, runtime=runtime
    )
    return mcp, runtime, rec


def script(tmp_path, turns):
    path = tmp_path / "turns.json"
    path.write_text(json.dumps(turns), encoding="utf-8")
    return f"script:{path}"


NOTEPAD = [
    {
        "tool_calls": [
            {"name": "App", "args": {"mode": "launch", "name": "Notepad"}},
            {"name": "Type", "args": {"target": "Text editor", "text": "milk, eggs"}},
            {"name": "Click", "args": {"target": "Save"}},
            {"name": "Done", "args": {"answer": "Grocery list typed.", "success": True}},
        ]
    }
]


def test_policy_classification():
    assert {"Do", "Route"} <= CLASSIFIED_TOOLS
    assert category("Do") == "direct" and is_screen_call("Do", {"goal": "x"})
    assert category("Route") == "other" and not is_screen_call("Route", {})
    assert requires_control("relaxed", "Do") and not requires_control("balanced", "Route")


@pytest.mark.asyncio
async def test_no_model_configured_is_a_clean_tool_error(monkeypatch):
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "WINDOWS_MCP_DO_MODEL",
        "WINDOWS_MCP_ROUTER_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    mcp, _, rec = build()
    async with Client(mcp) as client:
        result = await client.call_tool("Do", {"goal": "open notepad"}, raise_on_error=False)
        assert result.is_error
        assert "no model configured: anthropic needs ANTHROPIC_API_KEY" in result.content[0].text
        result = await client.call_tool("Route", {"text": "hi"}, raise_on_error=False)
        assert result.is_error and "OPENROUTER_API_KEY" in result.content[0].text
    assert rec.calls == []


@pytest.mark.asyncio
async def test_do_runs_inner_tools_under_its_screen_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_DO_MODEL", script(tmp_path, NOTEPAD))
    monkeypatch.setenv("WINDOWS_MCP_ROUTER_MODEL", "off")
    lock = ScreenLock()
    mcp, runtime, rec = build(lock=lock)
    async with Client(mcp) as client:
        do = asyncio.create_task(client.call_tool("Do", {"goal": "grocery list", "format": "json"}))
        await asyncio.sleep(0.03)  # Do now holds the lock (Snapshot/App done, Type running).
        outer = await client.call_tool("Click", {"target": "Outside"})
        result = await do
    summary = json.loads(result.content[0].text)
    assert summary["status"] == "done" and summary["ok"]
    assert [s["tool"] for s in summary["steps"]] == ["App", "Type", "Click"]
    assert summary["model"].startswith("script:")
    # The outsider's Click ran only after Do released the screen: never between its steps.
    names = [c[0] for c in rec.calls]
    assert names[-1] == "Click" and rec.calls[-1][1] == {"target": "Outside"}
    inner_click_end = rec.calls[-2][3]
    assert rec.calls[-1][2] >= inner_click_end
    assert "Outside" in outer.content[0].text
    assert not lock.locked and lock.status()["grants"] == 2  # Do once, outer Click once.
    events = [e for e in runtime.bus.history() if e["type"] == "do_event"]
    assert [e["event"] for e in events if e["event"] != "model"] == [
        "started",
        "step",
        "step",
        "step",
        "finished",
    ]
    assert events[-1]["ok"] is True and "steps" not in events[-1]


@pytest.mark.asyncio
async def test_do_text_transcript_and_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_DO_MODEL", script(tmp_path, NOTEPAD))
    monkeypatch.setenv("WINDOWS_MCP_ROUTER_MODEL", "off")
    mcp, _, _ = build()
    async with Client(mcp) as client:
        result = await client.call_tool("Do", {"goal": "grocery list"})
        text = result.content[0].text
        assert text.startswith("Do done (ok): Grocery list typed.")
        assert "[1] App" in text and "model" in text and "overhead" in text
        bad = await client.call_tool("Do", {"goal": " ", "max_steps": 3}, raise_on_error=False)
        assert bad.is_error
        bad = await client.call_tool("Do", {"goal": "x", "max_steps": 0}, raise_on_error=False)
        assert bad.is_error


def _gate_parts():
    pytest.importorskip("win32api")  # The real coordinator needs Windows.
    from windows_mcp.desktop.control import ControlCoordinator
    from windows_mcp.tools.control_notifications import ControlNotifier, ControlToolGate

    return ControlCoordinator, ControlNotifier, ControlToolGate


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["relaxed", "strict"])
async def test_do_inner_calls_are_gated_without_deadlock(tmp_path, monkeypatch, policy):
    from unittest.mock import Mock

    ControlCoordinator, ControlNotifier, ControlToolGate = _gate_parts()
    monkeypatch.setenv("WINDOWS_MCP_DO_MODEL", script(tmp_path, NOTEPAD))
    monkeypatch.setenv("WINDOWS_MCP_ROUTER_MODEL", "off")
    owner = ControlCoordinator()
    owner.enabled, owner.policy = True, policy
    owner.status = Mock(return_value={"state": "ready", "generation": 1})
    owner.begin_call = Mock(return_value=7)
    owner.checkpoint = Mock()
    owner.end_call = Mock()
    mcp, _, rec = build(gate=ControlToolGate(owner, ControlNotifier(owner)))
    async with Client(mcp) as client:
        result = await asyncio.wait_for(client.call_tool("Do", {"goal": "grocery list"}), 5)
    assert not result.is_error and "Do done (ok)" in result.content[0].text
    names = [c.args[0] for c in owner.begin_call.call_args_list]
    assert "Do" not in names and "Type" in names and "Click" in names
    assert ("Snapshot" in names) is (policy == "strict")


@pytest.mark.asyncio
async def test_do_rejected_during_user_takeover(tmp_path, monkeypatch):
    ControlCoordinator, ControlNotifier, ControlToolGate = _gate_parts()
    monkeypatch.setenv("WINDOWS_MCP_DO_MODEL", script(tmp_path, NOTEPAD))
    owner = ControlCoordinator()
    owner.policy = "relaxed"
    owner._state = "user"
    owner._last_user = time.monotonic()
    mcp, _, rec = build(gate=ControlToolGate(owner, ControlNotifier(owner)))
    async with Client(mcp) as client:
        result = await client.call_tool("Do", {"goal": "grocery list"}, raise_on_error=False)
    assert result.is_error and "USER_CONTROL" in result.content[0].text
    assert rec.calls == []


class FakeSession:
    def __init__(self, body):
        self.body = body
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json))

        class R:
            status_code = 200

            def json(_self):
                return self.body

        return R()


@pytest.mark.asyncio
async def test_route_tool_with_mocked_decision_api(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_OR)
    monkeypatch.delenv("WINDOWS_MCP_ROUTER_MODEL", raising=False)
    body = {
        "model": "microsoft/microsoft-decision-1-20261009",
        "answers": {
            "q": {
                "type": "choice",
                "choice": "needs_plan",
                "confidence": 0.8,
                "probabilities": {
                    "direct_action": 0.1,
                    "needs_plan": 0.85,
                    "answer_only": 0.03,
                    "clarify": 0.02,
                },
            }
        },
        "usage": {"input_tokens": 90, "output_tokens": 1, "cost": 0.000004},
    }
    fake = FakeSession(body)
    monkeypatch.setattr(model_http, "session", lambda: fake)
    mcp, _, _ = build()
    async with Client(mcp) as client:
        result = await client.call_tool(
            "Route", {"text": "write a note then play my favorite songs"}
        )
        text = result.content[0].text
        assert text.startswith("route: needs_plan, confidence 0.80")
        assert "needs_plan=0.85" in text
        raw = await client.call_tool(
            "Route",
            {"text": "x", "options": ["yes", "no"], "instructions": "Spam?", "format": "json"},
        )
        assert json.loads(raw.content[0].text)["model"].startswith("microsoft/")
        bad = await client.call_tool(
            "Route", {"mode": "verify", "after": "x"}, raise_on_error=False
        )
        assert bad.is_error and "expected" in bad.content[0].text
    url, sent = fake.posts[1]
    assert url == "https://openrouter.ai/api/alpha/decisions"
    assert sent["questions"]["q"]["criteria"] == {"yes": "yes", "no": "no"}
    assert sent["questions"]["q"]["instructions"] == "Spam?"
