"""Browser tool (CDP) with the DevTools connection mocked: modes, waits, guard gate, policy."""

from __future__ import annotations

import json
import re
import threading
import time

import pytest
from fastmcp import Client, FastMCP

from windows_mcp.browser import launcher
from windows_mcp.browser import service as browser_service
from windows_mcp.browser.cdp import CDPConnection, CDPDisconnected, CDPError
from windows_mcp.browser.service import BrowserService, decode_bing, parse_chord
from windows_mcp.guard import service as guard_service
from windows_mcp.guard.model import DecisionClient
from windows_mcp.guard.service import GuardService
from windows_mcp.tools import browser as browser_tools
from windows_mcp.tools.control_policy import (
    CLASSIFIED_TOOLS,
    category,
    is_screen_call,
    requires_control,
)

_CALL = re.compile(r'\)\("(\w+)", (.*)\)$', re.S)


class FakeConn:
    """Stands in for CDPConnection: scripted replies, a synchronous event buffer."""

    def __init__(self, url: str = "about:blank") -> None:
        self.sent: list[tuple[str, dict, str | None]] = []
        self.closed = False
        self._events: list[tuple[int, dict]] = []
        self._seq = 0
        self.listeners = []
        self.url = url
        self.title = "Page"
        self.has_password = False
        self.nav_error = None
        self.resolved = {
            "n": 1,
            "x": 10,
            "y": 20,
            "hit": True,
            "desc": {"n": 1, "role": "textbox", "label": "What needs to be done?"},
            "field": True,
        }
        self.evals: list[str] = []
        self.eval_value = None
        self.on_click_navigate: str | None = None

    # -- CDPConnection surface
    def on_event(self, listener) -> None:
        self.listeners.append(listener)

    def emit(self, method: str, params: dict, session: str | None = "S1") -> None:
        msg = {"method": method, "params": params}
        if session:
            msg["sessionId"] = session
        for listener in self.listeners:
            listener(msg)
        self._seq += 1
        self._events.append((self._seq, msg))

    def mark(self) -> int:
        return self._seq

    def wait_event(self, predicate, *, since: int, timeout: float):
        for seq, msg in self._events:
            if seq > since and predicate(msg):
                return msg
        return None

    def events_since(self, since: int) -> list[dict]:
        return [m for s, m in self._events if s > since]

    def send_nowait(self, method, params=None, *, session=None) -> None:
        self.sent.append((method, params or {}, session))

    def close(self) -> None:
        self.closed = True

    def send(self, method, params=None, *, session=None, timeout=10.0) -> dict:
        if self.closed:
            raise CDPDisconnected("closed")
        params = params or {}
        self.sent.append((method, params, session))
        handler = getattr(self, "on_" + method.replace(".", "_"), None)
        return handler(params) if handler else {}

    # -- scripted protocol
    def on_Target_getTargets(self, p):
        return {
            "targetInfos": [
                {"targetId": "T1", "type": "page", "url": self.url, "title": self.title}
            ]
        }

    def on_Target_attachToTarget(self, p):
        return {"sessionId": "S1"}

    def on_Page_getFrameTree(self, p):
        return {"frameTree": {"frame": {"id": "F1"}}}

    def on_Page_navigate(self, p):
        if self.nav_error:
            return {"frameId": "F1", "errorText": self.nav_error}
        self.url = p["url"]
        self.emit(
            "Page.lifecycleEvent", {"frameId": "F1", "loaderId": "L1", "name": "DOMContentLoaded"}
        )
        return {"frameId": "F1", "loaderId": "L1"}

    def on_Input_dispatchMouseEvent(self, p):
        if p["type"] == "mouseReleased" and self.on_click_navigate:
            self._navigate_from_page(self.on_click_navigate)
        return {}

    def on_Input_dispatchKeyEvent(self, p):
        if p.get("key") == "Enter" and p["type"] == "keyDown" and self.on_click_navigate:
            self._navigate_from_page(self.on_click_navigate)
        return {}

    def _navigate_from_page(self, url: str) -> None:
        self.emit("Page.frameRequestedNavigation", {"frameId": "F1", "url": url})
        self.url = url
        self.emit("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L2", "url": url}})
        self.emit(
            "Page.lifecycleEvent", {"frameId": "F1", "loaderId": "L2", "name": "DOMContentLoaded"}
        )

    def on_Page_captureScreenshot(self, p):
        return {"data": "/9j/AA=="}

    def on_Runtime_evaluate(self, p):
        expr = p["expression"]
        m = _CALL.search(expr)
        if not m:
            self.evals.append(expr)
            return {"result": {"value": self.eval_value}}
        op, args = m.group(1), json.loads(m.group(2))
        self.evals.append(op)
        return {"result": {"value": self.page(op, args)}}

    def page(self, op: str, a: dict):
        if op == "state":
            return {
                "url": self.url,
                "title": self.title,
                "ready": "complete",
                "hasPassword": self.has_password,
            }
        if op == "resolve":
            return {
                **self.resolved,
                "url": self.url,
                "title": self.title,
                "hasPassword": self.has_password,
            }
        if op == "focus":
            return {"focused": True}
        if op == "value":
            return {"value": "typed"}
        if op == "jsclick":
            if self.on_click_navigate:
                self._navigate_from_page(self.on_click_navigate)
            return {"ok": True}
        if op == "wait":
            return {"ok": True, "url": self.url, "title": self.title}
        if op == "read":
            return {
                "url": self.url,
                "title": self.title,
                "text": "Hello",
                "textLength": 5,
                "total": 2,
                "hasPassword": self.has_password,
                "elements": [
                    {"n": 1, "role": "textbox", "label": "Search", "value": "x"},
                    {"n": 2, "role": "link", "label": "Docs", "href": self.url + "docs"},
                ],
            }
        if op == "results":
            return {
                "url": self.url,
                "title": self.title,
                "results": [
                    {
                        "n": 1,
                        "title": "Todo app",
                        "href": "https://www.bing.com/ck/a?u=a1aHR0cHM6Ly90b2RvaXN0LmNvbS8",
                        "site": "todoist.com",
                    }
                ],
            }
        return None

    def keys(self) -> list[str]:
        return [
            p["key"]
            for m, p, _ in self.sent
            if m == "Input.dispatchKeyEvent" and p["type"] in ("keyDown", "rawKeyDown")
        ]

    def inserted(self) -> list[str]:
        return [p["text"] for m, p, _ in self.sent if m == "Input.insertText"]


@pytest.fixture
def conn():
    return FakeConn()


@pytest.fixture
def svc(conn):
    service = BrowserService(
        ensure=lambda url: ({"webSocketDebuggerUrl": "ws://fake", "Browser": "Edg/130.0"}, False),
        connect=lambda url: conn,
    )
    browser_service.set_service(service)
    yield service
    browser_service.set_service(None)


@pytest.fixture
def events(monkeypatch):
    monkeypatch.delenv("WINDOWS_MCP_GUARD", raising=False)
    published: list[dict] = []
    guard_service.set_service(
        GuardService(publish=published.append, model=DecisionClient(api_key=""))
    )
    yield published
    guard_service.set_service(None)


def build() -> FastMCP:
    mcp = FastMCP("browser-test")
    browser_tools.register(mcp, get_desktop=lambda: None, get_analytics=lambda: None)
    return mcp


async def call(args: dict):
    async with Client(build()) as client:
        result = await client.call_tool("Browser", args, raise_on_error=False)
    texts = [getattr(b, "text", None) for b in result.content]
    return result, next((t for t in texts if t), "")


def payload(text: str) -> dict:
    return json.loads(text.splitlines()[0])


# ---------------------------------------------------------------------- policy
def test_browser_is_background_and_ungated():
    assert "Browser" in CLASSIFIED_TOOLS
    for mode in browser_tools.MODES:
        args = {"mode": mode}
        assert category("Browser", args) == "other"
        assert not is_screen_call("Browser", args)
        assert not requires_control("relaxed", "Browser", args)
        assert not requires_control("balanced", "Browser", args)
    assert requires_control("strict", "Browser", {"mode": "read"})
    assert not GuardService.gated_call("Browser", {"mode": "fill"})  # Gated in-tool instead.


# ---------------------------------------------------------------------- helpers
def test_parse_chord():
    assert parse_chord("Enter") == (0, ("Enter", "Enter", 13, "\r"))
    mods, (key, code, vk, text) = parse_chord("ctrl+shift+t")
    assert mods == 2 | 8 and key == "T" and code == "KeyT" and vk == ord("T")
    assert parse_chord("a")[1] == ("a", "KeyA", 65, "a")
    assert parse_chord("F5")[1][2] == 116
    with pytest.raises(browser_service.BrowserError):
        parse_chord("hyper+x")


def test_decode_bing_redirect():
    href = "https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly90b2RvaXN0LmNvbS8&ntb=1"
    assert decode_bing(href) == "https://todoist.com/"
    assert decode_bing("https://example.com/x") == "https://example.com/x"


def test_format_read_lists_numbered_elements():
    text = browser_tools.format_read(FakeConn("https://ex.com/").page("read", {}))
    assert "url: https://ex.com/" in text
    assert '[1] textbox "Search" value="x"' in text
    assert '[2] link "Docs" -> /docs' in text


# ---------------------------------------------------------------------- modes
async def test_open_waits_for_its_loader_and_reports_loaded(svc, conn, events):
    result, text = await call({"mode": "open", "url": "example.com"})
    assert not result.is_error, text
    out = payload(text)
    assert out["url"] == "https://example.com" and out["loaded"] is True
    assert ("Page.navigate", {"url": "https://example.com"}, "S1") in conn.sent
    # Attach enabled lifecycle events and focus emulation on the tab session.
    methods = [m for m, _, s in conn.sent if s == "S1"]
    assert "Page.setLifecycleEventsEnabled" in methods
    assert "Emulation.setFocusEmulationEnabled" in methods


async def test_open_navigation_error_is_a_tool_error(svc, conn, events):
    conn.nav_error = "net::ERR_NAME_NOT_RESOLVED"
    result, text = await call({"mode": "open", "url": "https://nope.invalid"})
    assert result.is_error and "ERR_NAME_NOT_RESOLVED" in text


async def test_open_with_read_appends_page(svc, conn, events):
    result, text = await call({"mode": "open", "url": "https://ex.com/", "read": True})
    assert not result.is_error
    assert payload(text)["loaded"] is True and '[2] link "Docs"' in text


async def test_search_returns_decoded_results(svc, conn, events):
    result, text = await call({"mode": "search", "query": "free todo"})
    out = payload(text)
    assert out["url"].startswith("https://www.bing.com/search?q=free+todo")
    assert out["results"][0]["href"] == "https://todoist.com/"


async def test_fill_list_submits_each_item(svc, conn, events):
    result, text = await call(
        {
            "mode": "fill",
            "target": "What needs to be done?",
            "text": ["Milk", "Eggs", "Rice"],
            "submit": True,
        }
    )
    assert not result.is_error, text
    assert conn.inserted() == ["Milk", "Eggs", "Rice"]
    assert conn.keys() == ["Enter", "Enter", "Enter"]
    out = payload(text)
    assert out["count"] == 3 and out["submitted"] is True


async def test_fill_without_target_is_refused(svc, conn, events):
    result, text = await call({"mode": "fill", "text": "x"})
    assert result.is_error and "needs element" in text


async def test_click_uses_renderer_mouse_and_detects_navigation(svc, conn, events):
    await call({"mode": "open", "url": "https://ex.com/"})
    conn.on_click_navigate = "https://ex.com/next"
    result, text = await call({"mode": "click", "target": "Next"})
    out = payload(text)
    assert out["via"] == "mouse" and out["navigated"] is True and out["loaded"] is True
    assert out["url"] == "https://ex.com/next"
    kinds = [p["type"] for m, p, _ in conn.sent if m == "Input.dispatchMouseEvent"]
    assert kinds == ["mouseMoved", "mousePressed", "mouseReleased"]


async def test_click_falls_back_to_js_when_covered(svc, conn, events):
    conn.resolved = {**conn.resolved, "hit": False}
    result, text = await call({"mode": "click", "element": 1})
    assert payload(text)["via"] == "js" and "jsclick" in conn.evals


async def test_press_keys_and_eval(svc, conn, events):
    result, text = await call({"mode": "press", "keys": "Tab ctrl+a"})
    assert payload(text)["pressed"] == ["Tab", "ctrl+a"]
    assert conn.keys() == ["Tab", "Control", "a"]
    conn.eval_value = {"n": 6}
    result, text = await call({"mode": "eval", "script": "({n: 6})"})
    assert payload(text)["value"] == {"n": 6}


async def test_wait_and_screenshot(svc, conn, events):
    result, text = await call({"mode": "wait", "text": "Rice", "timeout": 2})
    assert payload(text)["ok"] is True
    async with Client(build()) as client:
        shot = await client.call_tool("Browser", {"mode": "screenshot"})
    assert any(getattr(b, "type", "") == "image" for b in shot.content)
    assert any(m == "Page.bringToFront" for m, _, _ in conn.sent)


async def test_dialogs_are_accepted_and_reported(svc, conn, events):
    await call({"mode": "open", "url": "https://ex.com/"})
    conn.emit("Page.javascriptDialogOpening", {"type": "alert", "message": "hi"})
    assert ("Page.handleJavaScriptDialog", {"accept": True, "promptText": ""}, "S1") in conn.sent
    result, text = await call({"mode": "read"})
    result, text = await call({"mode": "click", "element": 1})
    assert payload(text)["dialogs"] == [{"type": "alert", "message": "hi"}]


async def test_disconnect_reconnects_once(svc, events):
    conns = [FakeConn(), FakeConn()]
    conns[0].closed = True
    svc._connect = lambda url: conns.pop(0)
    result, text = await call({"mode": "open", "url": "https://ex.com/"})
    assert not result.is_error, text
    assert payload(text)["loaded"] is True


# ---------------------------------------------------------------------- guard
async def test_navigation_to_dangerous_page_is_flagged_not_blocked(svc, conn, events):
    result, text = await call({"mode": "open", "url": "https://rnicorsoft.com/login"})
    assert not result.is_error
    out = payload(text)
    assert out["guard"]["verdict"] == "dangerous" and out["guard"]["brand"] == "Microsoft"
    ev = [e for e in events if e["event"] == "verdict"]
    assert ev and ev[-1]["via"] == "cdp" and ev[-1]["domain"] == "rnicorsoft.com"
    # The desktop watcher's state is untouched.
    assert guard_service.get_service().current()[0] is None


async def test_fill_on_dangerous_page_needs_confirmation(svc, conn, events):
    await call({"mode": "open", "url": "https://rnicorsoft.com/login"})
    result, text = await call({"mode": "fill", "element": 1, "text": "hunter2"})
    assert result.is_error
    risk = json.loads(text)
    assert risk["code"] == "SCAM_GUARD_CONFIRM_REQUIRED" and conn.inserted() == []
    result, text = await call({"mode": "fill", "element": 1, "text": "hunter2", "confirmed": True})
    assert not result.is_error and conn.inserted() == ["hunter2"]
    assert any(e["event"] == "user_confirmed" and e["tool"] == "Browser" for e in events)


async def test_press_enter_and_eval_gated_but_tab_is_not(svc, conn, events):
    await call({"mode": "open", "url": "https://rnicorsoft.com/"})
    assert (await call({"mode": "press", "keys": "Enter"}))[0].is_error
    assert (await call({"mode": "eval", "script": "1"}))[0].is_error
    assert not (await call({"mode": "press", "keys": "Tab"}))[0].is_error


async def test_safe_page_is_not_gated_and_emits_nothing_in_risky_mode(svc, conn, events):
    await call({"mode": "open", "url": "https://www.microsoft.com/"})
    result, _ = await call({"mode": "fill", "element": 1, "text": "hello"})
    assert not result.is_error and events == []


async def test_guard_off_skips_checks(svc, conn, events, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_GUARD", "0")
    await call({"mode": "open", "url": "https://rnicorsoft.com/login"})
    result, _ = await call({"mode": "fill", "element": 1, "text": "x"})
    assert not result.is_error and events == []


# ---------------------------------------------------------------------- launcher
def test_launcher_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WINDOWS_MCP_BROWSER_PORT", "9333")
    monkeypatch.setenv("WINDOWS_MCP_BROWSER_PROFILE", str(tmp_path / "p"))
    assert launcher.browser_port() == 9333
    assert launcher.profile_dir() == tmp_path / "p"
    monkeypatch.setenv("WINDOWS_MCP_BROWSER_PORT", "junk")
    assert launcher.browser_port() == launcher.DEFAULT_PORT
    exe = tmp_path / "browser.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("WINDOWS_MCP_BROWSER", str(exe))
    assert launcher.find_executable() == str(exe)


def test_ensure_attaches_without_launching(monkeypatch):
    info = {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}
    monkeypatch.setattr(launcher, "probe", lambda port, timeout=0.3: info)
    monkeypatch.setattr(launcher, "launch", lambda *a, **k: pytest.fail("launched"))
    assert launcher.ensure_browser() == (info, False)


def test_ensure_launches_and_polls(monkeypatch, tmp_path):
    monkeypatch.setenv("WINDOWS_MCP_BROWSER_PROFILE", str(tmp_path / "prof"))
    info = {"webSocketDebuggerUrl": "ws://x"}
    probes = iter([None, None, info])
    launched = []

    class Proc:
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(launcher, "probe", lambda port, timeout=0.3: next(probes))
    monkeypatch.setattr(launcher, "find_executable", lambda: "msedge.exe")
    monkeypatch.setattr(launcher, "launch", lambda *a, **k: launched.append(a) or Proc())
    assert launcher.ensure_browser() == (info, True)
    exe, port, profile, url = launched[0]
    assert exe == "msedge.exe" and port == launcher.DEFAULT_PORT and profile == tmp_path / "prof"


# ---------------------------------------------------------------------- CDP client
class FakeSocket:
    def __init__(self) -> None:
        self.inbox: list[str] = []
        self.cond = threading.Condition()
        self.sent: list[dict] = []
        self.closed = False

    def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["method"] == "Fail.me":
            reply = {"id": msg["id"], "error": {"code": -32000, "message": "nope"}}
        else:
            reply = {"id": msg["id"], "result": {"echo": msg["params"]}}
        self.push({"method": "Some.event", "params": {"n": msg["id"]}})
        self.push(reply)

    def push(self, msg: dict) -> None:
        with self.cond:
            self.inbox.append(json.dumps(msg))
            self.cond.notify_all()

    def __iter__(self):
        while True:
            with self.cond:
                while not self.inbox and not self.closed:
                    self.cond.wait(0.05)
                if self.closed and not self.inbox:
                    return
                raw = self.inbox.pop(0)
            yield raw

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()


def test_cdp_connection_matches_replies_and_buffers_events():
    sock = FakeSocket()
    conn = CDPConnection("ws://x", connect=lambda url, timeout: sock)
    mark = conn.mark()
    assert conn.send("Echo.it", {"a": 1}, session="S9") == {"echo": {"a": 1}}
    assert sock.sent[0]["sessionId"] == "S9"
    ev = conn.wait_event(lambda m: m["method"] == "Some.event", since=mark, timeout=1.0)
    assert ev["params"] == {"n": 1}
    with pytest.raises(CDPError, match="nope"):
        conn.send("Fail.me")
    t0 = time.monotonic()
    assert conn.wait_event(lambda m: False, since=conn.mark(), timeout=0.1) is None
    assert time.monotonic() - t0 < 1.0
    conn.close()
    sock.close()
    deadline = time.monotonic() + 2
    while not conn.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(CDPDisconnected):
        conn.send("Echo.it")


def test_warm_is_opt_in(monkeypatch, svc, conn):
    monkeypatch.delenv("WINDOWS_MCP_BROWSER_WARM", raising=False)
    assert browser_tools.start_warm() is None
    monkeypatch.setenv("WINDOWS_MCP_BROWSER_WARM", "1")
    thread = browser_tools.start_warm()
    thread.join(5)
    assert (
        svc.session == "S1"
        and ("Target.attachToTarget", {"targetId": "T1", "flatten": True}, None) in conn.sent
    )


async def test_transient_network_error_is_retried_once(svc, conn, events):
    errors = ["net::ERR_NETWORK_ACCESS_DENIED"]
    original = conn.on_Page_navigate

    def flaky(p):
        if errors:
            errors.pop()
            return {"frameId": "F1", "errorText": "net::ERR_NETWORK_ACCESS_DENIED"}
        return original(p)

    conn.on_Page_navigate = flaky
    result, text = await call({"mode": "open", "url": "https://ex.com/"})
    assert not result.is_error, text
    assert sum(1 for m, _, _ in conn.sent if m == "Page.navigate") == 2
