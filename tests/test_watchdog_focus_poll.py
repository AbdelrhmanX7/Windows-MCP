"""Regression tests for issue #332 — focus is polled, never subscribed to.

The terminal crash in the report was a native fault inside `PumpEvents`: UIA
calling back into the Python focus handler against an interface that had been
torn down. No `except` can catch that, so the focus subscription is gone and
the watchdog polls `GetFocusedElement()` instead. Every COM call is now made
by us, and its failures (including E_UNEXPECTED) arrive as COMErrors that feed
the existing rebuild-with-backoff path.
"""

import types

import comtypes
import pytest

from windows_mcp.watchdog import service as watchdog_service
from windows_mcp.watchdog.service import FAIL_THRESHOLD, WatchDog

E_UNEXPECTED = -2147418113


def _com_error(hresult=E_UNEXPECTED):
    return comtypes.COMError(hresult, "Catastrophic failure", (None, None, None, 0, None))


class FakeElement:
    def __init__(self, runtime_id):
        self.runtime_id = runtime_id

    def GetRuntimeId(self):
        return list(self.runtime_id)


class FakeUIA:
    """Stand-in IUIAutomation that refuses any event subscription."""

    def __init__(self, focused=None):
        self.focused = focused
        self.calls = 0

    def GetFocusedElement(self):
        self.calls += 1
        if isinstance(self.focused, Exception):
            raise self.focused
        return self.focused

    def AddFocusChangedEventHandler(self, *args):
        raise AssertionError("focus must be polled, not subscribed to")

    def RemoveFocusChangedEventHandler(self, *args):
        raise AssertionError("focus must be polled, not subscribed to")


@pytest.fixture
def watchdog(monkeypatch):
    """A WatchDog with every COM touchpoint stubbed out."""
    monkeypatch.setattr(
        watchdog_service._AutomationClient,
        "instance",
        classmethod(lambda cls: types.SimpleNamespace(UIAutomationCore=object())),
    )
    monkeypatch.setattr(watchdog_service.comtypes, "CoInitialize", lambda: None)
    monkeypatch.setattr(watchdog_service.comtypes, "CoUninitialize", lambda: None)
    watchdog = WatchDog()
    watchdog.uia = FakeUIA()
    return watchdog


class TestFocusPolling:
    def test_callback_fires_only_when_focus_changes(self, watchdog):
        seen = []
        watchdog.set_focus_callback(seen.append)
        first, same, second = FakeElement((1, 2)), FakeElement((1, 2)), FakeElement((3, 4))

        for element in (first, same, second):
            watchdog.uia.focused = element
            watchdog._poll_focus()

        assert seen == [first, second], "an unchanged runtime id must not re-notify"

    def test_null_focus_is_ignored(self, watchdog):
        seen = []
        watchdog.set_focus_callback(seen.append)
        watchdog.uia.focused = None

        watchdog._poll_focus()

        assert seen == []
        assert watchdog._focus_fail_count == 0

    def test_only_the_runtime_id_is_retained(self, watchdog):
        """No COM pointer may outlive the apartment it was created in."""
        watchdog.set_focus_callback(lambda element: None)
        watchdog.uia.focused = FakeElement((7, 8))

        watchdog._poll_focus()

        assert watchdog._last_focus_id == (7, 8)


class TestFailuresAreContained:
    def test_catastrophic_failure_requests_rebuild_at_threshold(self, watchdog):
        watchdog.set_focus_callback(lambda element: None)
        watchdog.uia.focused = _com_error()

        for i in range(1, FAIL_THRESHOLD):
            watchdog._poll_focus()
            assert watchdog._focus_fail_count == i
            assert not watchdog._needs_rebuild.is_set(), f"rebuilt too early at failure {i}"

        watchdog._poll_focus()
        assert watchdog._needs_rebuild.is_set()

    def test_com_error_from_callback_counts_toward_rebuild(self, watchdog):
        def callback(element):
            raise _com_error(-2147220991)

        watchdog.set_focus_callback(callback)
        for i in range(FAIL_THRESHOLD):
            watchdog.uia.focused = FakeElement((i,))
            watchdog._poll_focus()

        assert watchdog._needs_rebuild.is_set()

    def test_non_com_error_from_callback_is_swallowed(self, watchdog):
        def callback(element):
            raise ValueError("consumer bug")

        watchdog.set_focus_callback(callback)
        watchdog.uia.focused = FakeElement((1,))

        watchdog._poll_focus()  # must not raise

        assert watchdog._focus_fail_count == 0
        assert not watchdog._needs_rebuild.is_set()

    def test_success_resets_the_fail_count(self, watchdog):
        watchdog.set_focus_callback(lambda element: None)
        watchdog.uia.focused = _com_error()
        for _ in range(FAIL_THRESHOLD - 1):
            watchdog._poll_focus()

        watchdog.uia.focused = FakeElement((1,))
        watchdog._poll_focus()

        assert watchdog._focus_fail_count == 0


class TestEventLoop:
    def test_loop_polls_and_never_subscribes(self, watchdog, monkeypatch):
        """FakeUIA raises if the loop tries to register a focus handler."""
        monkeypatch.setattr(watchdog_service, "FOCUS_POLL_SECONDS", 0.0)
        seen = []
        watchdog.set_focus_callback(seen.append)
        watchdog.uia.focused = FakeElement((1,))
        pumps = {"n": 0}

        def pump(timeout):
            pumps["n"] += 1
            if pumps["n"] >= 3:
                watchdog.is_running.clear()

        monkeypatch.setattr(watchdog_service.comtypes.client, "PumpEvents", pump)
        watchdog.is_running.set()

        watchdog._event_loop()

        assert watchdog.uia.calls == 3
        assert len(seen) == 1

    def test_loop_exits_for_rebuild_after_repeated_failures(self, watchdog, monkeypatch):
        monkeypatch.setattr(watchdog_service, "FOCUS_POLL_SECONDS", 0.0)
        watchdog.set_focus_callback(lambda element: None)
        watchdog.uia.focused = _com_error()
        monkeypatch.setattr(watchdog_service.comtypes.client, "PumpEvents", lambda timeout: None)
        watchdog.is_running.set()

        watchdog._event_loop()  # returns on its own once the threshold is hit

        assert watchdog._needs_rebuild.is_set()
        assert watchdog.uia.calls == FAIL_THRESHOLD


class TestCoInitializeFailure:
    def test_coinitialize_failure_retries_instead_of_killing_the_thread(
        self, watchdog, monkeypatch
    ):
        attempts = {"n": 0}
        uninit = {"n": 0}

        def co_initialize():
            attempts["n"] += 1
            if attempts["n"] >= 3:
                watchdog.is_running.clear()
            raise OSError("RPC_E_CHANGED_MODE")

        monkeypatch.setattr(watchdog_service.comtypes, "CoInitialize", co_initialize)
        monkeypatch.setattr(
            watchdog_service.comtypes, "CoUninitialize", lambda: uninit.__setitem__("n", 1)
        )
        monkeypatch.setattr(watchdog.is_running, "wait", lambda timeout: False)
        watchdog.is_running.set()

        watchdog._run()  # must return normally, not raise

        assert attempts["n"] == 3
        assert uninit["n"] == 0, "CoUninitialize must not pair with a failed CoInitialize"
