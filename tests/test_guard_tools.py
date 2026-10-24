"""Context / CheckLink tools and the scam-guard input gate over an in-process FastMCP client."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client, FastMCP

from windows_mcp.guard import service as guard_service
from windows_mcp.guard.model import DecisionClient
from windows_mcp.guard.service import GuardService
from windows_mcp.tools import guard as guard_tools
from windows_mcp.tools import register_all

FG = (4242, "Sign in to your account - Microsoft Edge", 77)


def fake_ctx(url, *, password=False, recipients=None, hwnd=FG[0], title=FG[1]):
    return {
        "hwnd": hwnd,
        "title": title,
        "process": "msedge.exe",
        "app": "browser",
        "product": "edge",
        "url": url,
        "domain": None,
        "focused": {"name": "Password", "control_type": "Edit", "is_password": password},
        "recipients": recipients or [],
        "page_text": None,
        "ts": 1e12,  # far future: always fresh
        "probe_ms": 3.2,
    }


@pytest.fixture
def svc(monkeypatch):
    events: list[dict] = []
    service = GuardService(publish=events.append, model=DecisionClient(api_key=""))
    service.events = events
    guard_service.set_service(service)
    monkeypatch.setattr(guard_tools, "_foreground", lambda: FG)
    yield service
    guard_service.set_service(None)


def build(svc, probe=None):
    mcp = FastMCP("guard-test")
    calls: list[tuple[str, dict]] = []

    @mcp.tool(name="Type")
    def fake_type(text: str, confirmed: bool | str = False) -> str:
        calls.append(("Type", {"text": text}))
        return f"typed {text}"

    @mcp.tool(name="Shortcut")
    def fake_shortcut(shortcut: str, confirmed: bool | str = False) -> str:
        calls.append(("Shortcut", {"shortcut": shortcut}))
        return f"pressed {shortcut}"

    mcp.add_middleware(
        guard_tools.GuardGate(
            lambda: svc, foreground=lambda: FG, probe=probe or (lambda fg: svc.current()[0])
        )
    )
    return mcp, calls


async def call(client, name, args):
    result = await client.call_tool(name, args, raise_on_error=False)
    return result, (result.content[0].text if result.content else "")


async def test_gate_refuses_typing_into_dangerous_page_until_confirmed(svc):
    svc.evaluate(fake_ctx("https://rnicorsoft.com/login", password=True))
    mcp, calls = build(svc)
    async with Client(mcp) as client:
        result, text = await call(client, "Type", {"text": "hunter2"})
        assert result.is_error
        payload = json.loads(text)
        assert payload["code"] == "SCAM_GUARD_CONFIRM_REQUIRED"
        assert payload["domain"] == "rnicorsoft.com" and payload["brand"] == "Microsoft"
        assert calls == []
        result, text = await call(client, "Type", {"text": "hunter2", "confirmed": True})
        assert not result.is_error and calls == [("Type", {"text": "hunter2"})]
        assert svc.events[-1]["event"] == "user_confirmed"
        # Harmless shortcuts pass; send/submit shortcuts are gated.
        result, _ = await call(client, "Shortcut", {"shortcut": "ctrl+t"})
        assert not result.is_error
        result, _ = await call(client, "Shortcut", {"shortcut": "ctrl+enter"})
        assert result.is_error


async def test_gate_allows_official_and_unknown_sites(svc):
    mcp, calls = build(svc)
    async with Client(mcp) as client:
        svc.evaluate(fake_ctx("https://login.microsoftonline.com/", password=True))
        result, _ = await call(client, "Type", {"text": "x"})
        assert not result.is_error
        svc.evaluate(fake_ctx("https://some-blog.example/"))
        result, _ = await call(client, "Type", {"text": "y"})
        assert not result.is_error
    assert len(calls) == 2


async def test_gate_reprobes_when_foreground_changed(svc):
    # Cached context is an old safe window; the gate must re-read before typing.
    svc.evaluate(fake_ctx("https://www.microsoft.com/", hwnd=1, title="old"))
    probes = []

    def probe(fg):
        probes.append(fg)
        return fake_ctx("https://paypa1.com/signin", password=True)

    mcp, calls = build(svc, probe=probe)
    async with Client(mcp) as client:
        result, text = await call(client, "Type", {"text": "x"})
    assert probes == [FG]
    assert result.is_error and json.loads(text)["domain"] == "paypa1.com"
    assert calls == []


async def test_gate_blocks_send_to_lookalike_recipient(svc):
    ctx = fake_ctx(None, recipients=["support@rnicorsoft.com"])
    ctx.update(process="outlook.exe", app="email", title=FG[1])
    svc.evaluate(ctx)
    mcp, calls = build(svc)
    async with Client(mcp) as client:
        result, text = await call(client, "Shortcut", {"shortcut": "alt+s"})
    payload = json.loads(text)
    assert result.is_error and payload["source"] == "email"
    assert payload["recipient"] == "support@rnicorsoft.com"


async def test_check_link_and_context_tools(svc, monkeypatch):
    mcp = FastMCP("guard-tools")
    guard_tools.register(mcp, get_desktop=lambda: None, get_analytics=lambda: None)
    monkeypatch.setattr(
        guard_tools, "_probe_sync", lambda fg, text="auto": fake_ctx("https://rnicorsoft.com/")
    )
    async with Client(mcp) as client:
        result, text = await call(client, "CheckLink", {"target": "rnicorsoft.com"})
        one = json.loads(text)
        assert one["verdict"] == "dangerous" and one["brand"] == "Microsoft" and one["ms"] < 50
        result, text = await call(
            client, "CheckLink", {"target": ["github.com", "support@rnicorsoft.com"]}
        )
        many = json.loads(text)["results"]
        assert [r["verdict"] for r in many] == ["safe", "dangerous"]
        result, text = await call(client, "Context", {})
        ctx = json.loads(text)
        assert ctx["url"] == "https://rnicorsoft.com/" and ctx["guard"]["verdict"] == "dangerous"
        assert ctx["source"] == "browser" and ctx["cached"] is False
        result, text = await call(client, "Context", {})
        assert json.loads(text)["cached"] is True
        result, _ = await call(client, "CheckLink", {"target": []})
        assert result.is_error


def test_middleware_order_orchestration_then_guard_then_control_gate():
    class ControlGateStandIn:
        async def on_call_tool(self, context, call_next):  # pragma: no cover
            return await call_next(context)

    mcp = FastMCP("order")
    stand_in = ControlGateStandIn()
    mcp.middleware.append(stand_in)  # __main__ adds the control gate before register_all.
    register_all(mcp, get_desktop=lambda: None, get_analytics=lambda: None)
    names = [type(m).__name__ for m in mcp.middleware]
    assert names.index("OrchestrationMiddleware") < names.index("GuardGate")
    assert names.index("GuardGate") < names.index("ControlGateStandIn")


def test_guard_off_installs_no_gate(monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_GUARD", "0")
    mcp = FastMCP("off")
    register_all(mcp, get_desktop=lambda: None, get_analytics=lambda: None)
    assert "GuardGate" not in [type(m).__name__ for m in mcp.middleware]
    assert guard_tools.start_guard() is None


def test_input_tools_advertise_confirmed():
    mcp = FastMCP("schema")
    register_all(mcp, get_desktop=lambda: None, get_analytics=lambda: None)
    import asyncio

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in ("Type", "Click", "Shortcut", "MultiEdit"):
        assert "confirmed" in tools[name].parameters["properties"], name
    for name in ("Context", "CheckLink"):
        dumped = tools[name].annotations.model_dump()
        assert dumped.get("read_only_hint", dumped.get("readOnlyHint")) is True


async def test_run_parallel_inner_calls_are_gated(svc):
    from windows_mcp.tasks.runtime import TaskRuntime
    from windows_mcp.tasks.screen_lock import ScreenLock
    from windows_mcp.tools import tasks as tasks_module

    svc.evaluate(fake_ctx("https://rnicorsoft.com/login", password=True))
    mcp, calls = build(svc)
    tasks_module.register(
        mcp,
        get_desktop=lambda: None,
        get_analytics=lambda: None,
        runtime=TaskRuntime.create(screen_lock=ScreenLock()),
    )
    async with Client(mcp) as client:
        result, text = await call(
            client,
            "RunParallel",
            {"calls": [{"tool": "Type", "args": {"text": "hunter2"}}], "format": "json"},
        )
    assert "SCAM_GUARD_CONFIRM_REQUIRED" in text
    assert calls == []
