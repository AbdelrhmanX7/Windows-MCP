"""Actions return when the UI is ready; Wait returns early when nothing is in flight."""

import asyncio
from collections.abc import Callable

import pytest

from windows_mcp.desktop import ready
from windows_mcp.desktop.ready import (
    READY_TIMEOUT_ENV,
    Readiness,
    Sample,
    WaitTracker,
    ready_timeout,
)
from windows_mcp.tools import input as input_tools

EDGE = Sample(hwnd=1, title="TodoMVC - Microsoft Edge", browser=True)
NOTEPAD = Sample(hwnd=2, title="Untitled - Notepad")
SAVE_AS = Sample(hwnd=3, title="Save As")


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def timeline(clock: Clock, events: list[tuple[float, Sample]]) -> Callable[[bool], Sample]:
    """Sample source: the last event whose start offset (s) has passed."""
    start = clock.now

    def sample(deep: bool = False) -> Sample:
        current = events[0][1]
        for at, value in events:
            if clock.now - start >= at - 1e-9:
                current = value
        return current

    return sample


def make(clock: Clock, events, timeout: float = 3.0) -> Readiness:
    return Readiness(
        sample=timeline(clock, events), clock=clock, sleep=clock.sleep, timeout=lambda: timeout
    )


class TestReadyTimeout:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(READY_TIMEOUT_ENV, raising=False)
        assert ready_timeout() == ready.DEFAULT_READY_TIMEOUT_S

    @pytest.mark.parametrize(("raw", "value"), [("0", 0.0), ("1.5", 1.5), ("999", 30.0)])
    def test_reads_and_clamps(self, monkeypatch, raw, value):
        monkeypatch.setenv(READY_TIMEOUT_ENV, raw)
        assert ready_timeout() == value

    @pytest.mark.parametrize("raw", ["", "soon", "-1", "nan"])
    def test_invalid_uses_default(self, monkeypatch, raw):
        monkeypatch.setenv(READY_TIMEOUT_ENV, raw)
        assert ready_timeout() == ready.DEFAULT_READY_TIMEOUT_S


class TestAfter:
    def test_nothing_changed_returns_after_one_sample(self):
        clock = Clock()
        r = make(clock, [(0, EDGE)])
        assert r.after(EDGE) is None
        assert clock.slept == []

    def test_watch_looks_for_a_late_change_then_gives_up(self):
        clock = Clock()
        r = make(clock, [(0, NOTEPAD)])
        assert r.after(NOTEPAD, watch=0.5) is None
        assert 0.5 <= clock.now - 100.0 < 0.6

    def test_waits_for_page_load(self):
        clock = Clock()
        loading = Sample(hwnd=1, title=EDGE.title, browser=True, loading=True)
        loaded = Sample(hwnd=1, title="todo list - Search - Microsoft Edge", browser=True)
        r = make(clock, [(0, loading), (0.8, loaded)])
        settled = r.after(EDGE)
        assert settled.state == "loaded"
        assert settled.note.startswith('Page loaded: "todo list - Search')
        assert 0.8 <= settled.elapsed < 1.0

    def test_timeout_says_still_loading(self):
        clock = Clock()
        loading = Sample(hwnd=1, title=EDGE.title, browser=True, loading=True)
        settled = make(clock, [(0, loading)], timeout=1.0).after(EDGE)
        assert settled.state == "loading"
        assert "still loading after 1.0 s" in settled.note
        assert clock.now - 100.0 == pytest.approx(1.0, abs=0.05)

    def test_dialog_opened(self):
        clock = Clock()
        r = make(clock, [(0, NOTEPAD), (0.09, SAVE_AS)])
        settled = r.after(NOTEPAD, watch=0.5)
        assert settled.state == "window"
        assert settled.note.startswith('Window "Save As" is now in front')
        assert settled.elapsed < 0.25

    def test_launch_waits_for_title_to_hold_still_but_not_for_page_load(self):
        clock = Clock()
        starting = Sample(hwnd=5, title="Microsoft Edge", browser=True, loading=True)
        titled = Sample(hwnd=5, title="New tab - Microsoft Edge", browser=True, loading=True)
        r = make(clock, [(0, starting), (0.1, titled)])
        settled = r.after(Sample(), launched=True, timeout=1.5)
        assert settled.state == "ready"
        assert '"New tab - Microsoft Edge"' in settled.note
        assert settled.elapsed < 0.4

    def test_disabled(self):
        clock = Clock()
        r = make(clock, [(0, EDGE)], timeout=0.0)
        assert r.before() is None
        assert r.after(EDGE) is None

    def test_checkpoint_runs_on_every_poll(self):
        clock = Clock()
        calls = []
        r = make(clock, [(0, NOTEPAD), (0.1, SAVE_AS)])
        r.after(NOTEPAD, watch=0.5, checkpoint=lambda: calls.append(1))
        assert len(calls) >= 3

    def test_sample_errors_read_as_idle(self):
        def broken(deep: bool = False) -> Sample:
            raise OSError("no desktop")

        r = Readiness(sample=broken, timeout=lambda: 3.0)
        assert r.sample() == Sample()


class TestWaitTracker:
    def run(self, clock: Clock, events, duration: float) -> tuple[float, str]:
        r = make(clock, events)
        return r.wait(duration)

    def test_idle_returns_after_minimum(self):
        clock = Clock()
        waited, message = self.run(clock, [(0, NOTEPAD)], 3)
        assert waited == pytest.approx(0.1, abs=0.04)
        assert message.startswith("UI already idle")

    def test_keeps_waiting_while_loading(self):
        clock = Clock()
        loading = Sample(hwnd=1, title=EDGE.title, browser=True, loading=True)
        waited, message = self.run(clock, [(0, loading), (1.2, EDGE)], 3)
        assert 1.2 <= waited < 1.5
        assert "page finished loading" in message

    def test_honors_full_duration_when_never_idle(self):
        clock = Clock()
        loading = Sample(hwnd=1, title=EDGE.title, browser=True, loading=True)
        waited, message = self.run(clock, [(0, loading)], 2)
        assert waited == pytest.approx(2.0, abs=0.05)
        assert "still loading" in message

    def test_window_change_during_wait_is_waited_out(self):
        clock = Clock()
        waited, message = self.run(clock, [(0, NOTEPAD), (0.05, SAVE_AS)], 3)
        assert 0.15 <= waited < 0.3
        assert "UI settled" in message

    def test_zero_duration(self):
        tracker = WaitTracker(0.0, 0)
        waited, message = tracker.update(NOTEPAD, 0.0)
        assert waited == 0.0


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self, *, name: str, **kwargs: object) -> Callable:
        def decorator(func: Callable) -> Callable:
            self.tools[name] = func
            return func

        return decorator


class FakeDesktop:
    def __init__(self) -> None:
        self.desktop_state = object()
        self.type_calls: list[dict] = []
        self.shortcuts: list[str] = []

    def type(self, **kwargs) -> None:
        self.type_calls.append(kwargs)

    def shortcut(self, shortcut: str) -> None:
        self.shortcuts.append(shortcut)


@pytest.fixture
def tools(monkeypatch):
    clock = Clock()
    holder = {"events": [(0, EDGE)]}

    def sample(deep: bool = False) -> Sample:
        return timeline(clock, holder["events"])(deep)

    fake = Readiness(sample=sample, clock=clock, sleep=clock.sleep, timeout=lambda: 3.0)
    monkeypatch.setattr(input_tools, "readiness", fake)
    monkeypatch.setattr(input_tools, "_focused_name", lambda: "New Todo Input")
    desktop = FakeDesktop()
    mcp = FakeMCP()
    input_tools.register(mcp, get_desktop=lambda: desktop, get_analytics=lambda: None)
    return mcp.tools, desktop, holder, clock


def test_type_without_location_types_into_the_focused_element(tools):
    registered, desktop, _, _ = tools
    result = asyncio.run(registered["Type"](text="Milk", press_enter=True))
    assert desktop.type_calls[0]["loc"] is None
    assert desktop.type_calls[0]["text"] == "Milk"
    assert result == 'Typed Milk into the focused element "New Todo Input". Enter pressed.'


def test_type_strips_a_trailing_newline_when_press_enter(tools):
    registered, desktop, _, _ = tools
    asyncio.run(registered["Type"](text="query\n", press_enter=True))
    assert desktop.type_calls[0]["text"] == "query"


def test_type_still_rejects_a_bad_location(tools):
    registered, _, _, _ = tools
    with pytest.raises(ValueError, match="exactly 2 integers"):
        asyncio.run(registered["Type"](text="x", loc=[1, 2, 3]))


def test_type_enter_reports_page_load(tools):
    registered, _, holder, _ = tools
    loading = Sample(hwnd=1, title=EDGE.title, browser=True, loading=True)
    holder["events"] = [(0, loading), (0.5, Sample(hwnd=1, title="Bing", browser=True))]
    result = asyncio.run(registered["Type"](text="q", loc=[10, 10], press_enter=True))
    assert result.startswith('Typed q at (10,10). Enter pressed. Page loaded: "Bing"')


def test_plain_type_does_not_sample(tools, monkeypatch):
    registered, _, _, _ = tools

    def fail(*args, **kwargs):
        raise AssertionError("plain typing must not wait for readiness")

    monkeypatch.setattr(input_tools.readiness, "after", fail)
    assert asyncio.run(registered["Type"](text="a", loc=[1, 1])) == "Typed a at (1,1)."


def test_shortcut_reports_dialog(tools):
    registered, desktop, holder, _ = tools
    holder["events"] = [(0, NOTEPAD), (0.09, SAVE_AS)]
    result = asyncio.run(registered["Shortcut"](shortcut="Ctrl+S"))
    assert desktop.shortcuts == ["Ctrl+S"]
    assert result.startswith('Pressed Ctrl+S. Window "Save As" is now in front')


def test_editing_shortcut_takes_one_sample(tools):
    registered, _, holder, clock = tools
    holder["events"] = [(0, NOTEPAD)]
    assert asyncio.run(registered["Shortcut"](shortcut="ctrl+v")) == "Pressed ctrl+v."
    assert clock.slept == []


def test_wait_returns_early_when_idle(tools, monkeypatch):
    registered, _, holder, _ = tools
    holder["events"] = [(0, NOTEPAD)]
    started = asyncio.run(_timed(registered["Wait"](duration=3)))
    elapsed, message = started
    assert message.startswith("UI already idle")
    assert elapsed < 1.0


def test_wait_sleeps_fully_when_disabled(tools, monkeypatch):
    registered, _, _, _ = tools
    monkeypatch.setenv(READY_TIMEOUT_ENV, "0")
    elapsed, message = asyncio.run(_timed(registered["Wait"](duration=0.3)))
    assert message == "Waited for 0.3 seconds."
    assert elapsed >= 0.29


def test_wait_rejects_bad_duration(tools):
    registered, _, _, _ = tools
    with pytest.raises(ValueError):
        asyncio.run(registered["Wait"](duration=-1))


async def _timed(coro) -> tuple[float, str]:
    import time

    start = time.monotonic()
    result = await coro
    return time.monotonic() - start, result
