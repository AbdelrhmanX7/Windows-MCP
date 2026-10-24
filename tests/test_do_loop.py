"""The Do agent loop with a scripted model and fake in-process tools (pure Python)."""

from __future__ import annotations

import asyncio
import time

import pytest

from windows_mcp.agent.loop import DoRun, format_text
from windows_mcp.agent.tools import DONE, INNER_TOOLS, inner_args, menu
from windows_mcp.models.chat import ScriptedChat
from windows_mcp.models.config import ModelError
from windows_mcp.models.router import Decision


def turn(*calls, text=""):
    return {"text": text, "tool_calls": [{"name": n, "args": a} for n, a in calls]}


class FakeTools:
    def __init__(self, fail=None, delay=0.0, snapshots=None):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail or {}
        self.delay = delay
        self.snapshots = list(snapshots or [])

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if self.delay:
            await asyncio.sleep(self.delay)
        if name in self.fail:
            raise RuntimeError(self.fail[name])
        if name == "Snapshot":
            return self.snapshots.pop(0) if self.snapshots else "Focused Window: Desktop"
        return f"{name} ok"


class FakeRouter:
    def __init__(self, *choices):
        self.choices = list(choices)
        self.seen = []

    def verify(self, before, after, expected, actions=None, timeout=None):
        self.seen.append((before, after, expected))
        self.actions = actions
        picked = self.choices.pop(0)
        return Decision(picked, {picked: 1.0}, 0.9, 5.0, "fake", {"cost": 0.000001})


def run(goal, turns, tools=None, **kw):
    tools = tools or FakeTools()
    events = []

    async def emit(event):
        events.append(event)

    loop = DoRun(goal, client=ScriptedChat(turns), call_tool=tools, emit=emit, **kw)
    summary = asyncio.run(loop.run())
    return summary, tools, events, loop


def test_one_turn_batch_then_done():
    summary, tools, events, _ = run(
        "open notepad and type milk",
        [
            turn(
                ("App", {"mode": "launch", "name": "Notepad"}),
                ("Type", {"target": "Text editor", "text": "milk", "bogus": 1}),
                (DONE, {"answer": "Typed it.", "success": True}),
            )
        ],
    )
    assert summary["status"] == "done" and summary["ok"] and summary["answer"] == "Typed it."
    assert summary["turns"] == 1 and summary["step_count"] == 2
    # First call is the observation snapshot, pinned to no vision/annotation.
    assert tools.calls[0] == ("Snapshot", {"use_vision": False, "use_annotation": False})
    assert tools.calls[2] == ("Type", {"target": "Text editor", "text": "milk"})
    assert [s["tool"] for s in summary["steps"]] == ["App", "Type"]
    total = summary["model_ms"] + summary["tool_ms"] + summary["verify_ms"]
    assert summary["overhead_ms"] == pytest.approx(summary["total_ms"] - total, abs=0.2)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "started" and kinds[-1] == "finished"
    assert kinds.count("step") == 2 and "model" in kinds
    assert all(e["type"] == "do_event" and e["run_id"] == summary["run_id"] for e in events)
    assert "steps" not in events[-1]
    text = format_text(summary)
    assert "Do done (ok): Typed it." in text and "[2] Type" in text


def test_failed_call_skips_rest_of_turn_and_model_recovers():
    tools = FakeTools(fail={"Click": "Not clicked: no match for 'Sav'"})
    summary, tools, _, _ = run(
        "save",
        [
            turn(
                ("Click", {"target": "Sav"}),
                ("Shortcut", {"shortcut": "enter"}),
                (DONE, {"answer": "x", "success": True}),
            ),
            turn(
                ("Shortcut", {"shortcut": "ctrl+s"}), (DONE, {"answer": "Saved", "success": True})
            ),
        ],
        tools=tools,
    )
    names = [c[0] for c in tools.calls]
    assert names == ["Snapshot", "Click", "Shortcut"]  # enter + first Done were skipped
    assert summary["status"] == "done" and summary["turns"] == 2
    assert summary["steps"][0]["ok"] is False and "no match" in summary["steps"][0]["error"]


def test_control_errors_abort_immediately():
    tools = FakeTools(fail={"Click": "USER_CONTROL: the user is using the desktop"})
    summary, tools, _, _ = run(
        "x", [turn(("Click", {"target": "OK"})), turn(("Click", {"target": "OK"}))], tools=tools
    )
    assert summary["status"] == "blocked" and "USER_CONTROL" in summary["answer"]
    assert summary["turns"] == 1


def test_max_steps_and_unknown_tools():
    summary, tools, _, _ = run(
        "x",
        [
            turn(
                ("Shortcut", {"shortcut": "a"}),
                ("Shortcut", {"shortcut": "b"}),
                ("Shortcut", {"shortcut": "c"}),
            )
        ],
        max_steps=2,
    )
    assert summary["status"] == "max_steps" and summary["step_count"] == 2
    summary, tools, _, _ = run(
        "x", [turn(("RunParallel", {"calls": []})), turn(text="cannot")], observe=False
    )
    assert tools.calls == [] and summary["answer"] == "cannot"


def test_menu_honors_registered_tools_and_never_nests():
    names = [t.name for t in menu({"Click", "Type", "Snapshot", "RunParallel", "Do"})]
    assert names == ["Snapshot", "Click", "Type", DONE]
    assert "PowerShell" not in [t.name for t in menu({"Click"})]
    assert {t.name for t in menu(None)} == set(INNER_TOOLS) | {DONE}
    assert inner_args("Wait", {"duration": 60}) == {"duration": 5}
    assert inner_args("Scroll", {"direction": "left"}) == {
        "direction": "left",
        "type": "horizontal",
    }
    # Excluded tool requested by the model is refused, not called.
    summary, tools, _, _ = run(
        "x",
        [turn(("PowerShell", {"command": "rm -r C:/"})), turn(text="ok")],
        available={"Click", "Snapshot"},
    )
    assert [c[0] for c in tools.calls] == ["Snapshot"]


def test_verifier_failed_feeds_back_then_succeeds_with_still_loading_retry():
    router = FakeRouter("failed", "still_loading", "succeeded")
    tools = FakeTools(snapshots=["before", "after-1", "after-2", "after-3"])
    summary, tools, events, _ = run(
        "open calc",
        [
            turn(
                ("App", {"name": "Calculator"}),
                (DONE, {"answer": "Open", "success": True, "expect": "Calculator window"}),
            ),
            turn(
                ("WaitFor", {"condition": "active_window", "window_name": "Calculator"}),
                (DONE, {"answer": "Open now", "success": True, "expect": "Calculator window"}),
            ),
        ],
        tools=tools,
        router=router,
    )
    assert summary["status"] == "done" and summary["answer"] == "Open now"
    assert [v["choice"] for v in summary["verifications"]] == [
        "failed",
        "still_loading",
        "succeeded",
    ]
    assert router.seen[0] == ("before", "after-1", "Calculator window")
    assert summary["verify_ms"] >= 0 and summary["turns"] == 2
    assert summary["verified"] is True
    assert sum(e["event"] == "verify" for e in events) == 3


def test_two_verifier_rejections_report_unverified():
    router = FakeRouter("failed", "failed")
    done = (DONE, {"answer": "Typed", "success": True, "expect": "milk in Notepad"})
    summary, *_ = run(
        "type milk",
        [turn(("Type", {"target": "Text Editor", "text": "milk"}), done), turn(done)],
        router=router,
    )
    assert summary["status"] == "done" and summary["ok"] and summary["verified"] is False
    assert summary["turns"] == 2
    assert "Type" in router.actions[0] and "milk" in router.actions[0]
    assert "did not confirm" in format_text(summary)


def test_done_without_expect_or_success_false():
    router = FakeRouter()
    summary, *_ = run("x", [turn((DONE, {"answer": "no", "success": False}))], router=router)
    assert summary["status"] == "failed" and not summary["ok"]
    summary, *_ = run("x", [turn((DONE, {"answer": "yes", "success": True}))], router=router)
    assert summary["status"] == "done" and router.seen == []


def test_model_error_and_timeout():
    class Broken(ScriptedChat):
        def chat(self, *a, **k):
            raise ModelError("anthropic returned HTTP 529: overloaded")

    loop = DoRun("x", client=Broken([]), call_tool=FakeTools(), observe=False)
    summary = asyncio.run(loop.run())
    assert summary["status"] == "error" and "529" in summary["answer"]

    slow = ScriptedChat([turn(("Shortcut", {"shortcut": "a"}))] * 10, latency_ms=400)
    loop = DoRun("x", client=slow, call_tool=FakeTools(), observe=False, timeout=1.0, max_steps=10)
    t0 = time.perf_counter()
    summary = asyncio.run(loop.run())
    assert summary["status"] == "timeout" and time.perf_counter() - t0 < 2.5


def test_overhead_per_step_is_small():
    turns = [
        turn(*[("Shortcut", {"shortcut": "a"})] * 8, (DONE, {"answer": "ok", "success": True}))
    ]
    summary, *_ = run("x", turns, max_steps=8)
    assert summary["step_count"] == 8
    assert summary["overhead_per_step_ms"] < 20
