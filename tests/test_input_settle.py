"""Post-input settle delay and the double-click guard that replaced the 0.5 s sleeps."""

import sys
from unittest.mock import MagicMock

import pytest

from windows_mcp.uia import settle
from windows_mcp.uia.settle import INPUT_SETTLE_ENV, input_settle_seconds


class TestInputSettleSeconds:
    def test_default_is_short(self, monkeypatch):
        monkeypatch.delenv(INPUT_SETTLE_ENV, raising=False)
        assert input_settle_seconds() == pytest.approx(settle.DEFAULT_INPUT_SETTLE_MS / 1000)
        assert input_settle_seconds() < 0.1

    @pytest.mark.parametrize(("raw", "seconds"), [("500", 0.5), ("0", 0.0), (" 12.5 ", 0.0125)])
    def test_reads_milliseconds(self, monkeypatch, raw, seconds):
        monkeypatch.setenv(INPUT_SETTLE_ENV, raw)
        assert input_settle_seconds() == pytest.approx(seconds)

    def test_legacy_value_restores_old_timing(self, monkeypatch):
        monkeypatch.setenv(INPUT_SETTLE_ENV, str(int(settle.LEGACY_INPUT_SETTLE_MS)))
        assert input_settle_seconds() == pytest.approx(0.5)

    @pytest.mark.parametrize("raw", ["", "fast", "-5", "nan", "inf"])
    def test_invalid_values_fall_back_to_default(self, monkeypatch, raw):
        monkeypatch.setenv(INPUT_SETTLE_ENV, raw)
        assert input_settle_seconds() == pytest.approx(settle.DEFAULT_INPUT_SETTLE_MS / 1000)

    def test_huge_values_are_clamped(self, monkeypatch):
        monkeypatch.setenv(INPUT_SETTLE_ENV, "999999")
        assert input_settle_seconds() == pytest.approx(settle.MAX_INPUT_SETTLE_MS / 1000)


@pytest.mark.skipif(sys.platform != "win32", reason="uia.core loads user32")
class TestCoreHelpersUseSettle:
    @pytest.fixture
    def core(self, monkeypatch):
        from windows_mcp.uia import core

        sleeps: list[float] = []
        monkeypatch.setattr(core.time, "sleep", sleeps.append)
        monkeypatch.setattr(core, "mouse_event", lambda *a: None)
        monkeypatch.setattr(core, "keybd_event", lambda *a: None)
        monkeypatch.setattr(core, "SendUnicodeChar", lambda *a: None)
        monkeypatch.setattr(core, "SetCursorPos", lambda x, y: True)
        monkeypatch.setattr(core, "GetVirtualScreenRect", lambda: (0, 0, 1920, 1080))
        core.sleeps = sleeps
        yield core
        del core.sleeps

    def test_click_sleeps_the_configured_settle(self, core, monkeypatch):
        monkeypatch.setenv(INPUT_SETTLE_ENV, "40")
        core.Click(10, 10)
        assert core.sleeps == [0.05, pytest.approx(0.04)]

    def test_explicit_wait_time_still_wins(self, core, monkeypatch):
        monkeypatch.setenv(INPUT_SETTLE_ENV, "40")
        core.Click(10, 10, waitTime=0.3)
        assert core.sleeps[-1] == 0.3

    def test_sendkeys_ends_with_the_settle(self, core, monkeypatch):
        monkeypatch.setenv(INPUT_SETTLE_ENV, "500")
        core.SendKeys("{Ctrl}a", interval=0)
        assert core.sleeps[-1] == pytest.approx(0.5)

    @pytest.mark.parametrize("name", ["RightClick", "MiddleClick"])
    def test_other_buttons(self, core, monkeypatch, name):
        monkeypatch.setenv(INPUT_SETTLE_ENV, "7")
        getattr(core, name)(1, 1)
        assert core.sleeps[-1] == pytest.approx(0.007)

    def test_wheel(self, core, monkeypatch):
        monkeypatch.setenv(INPUT_SETTLE_ENV, "7")
        core.WheelDown(1, interval=0)
        assert core.sleeps[-1] == pytest.approx(0.007)


@pytest.mark.skipif(sys.platform != "win32", reason="input_timing imports pywin32")
class TestDoubleClickGuard:
    @pytest.fixture
    def guard(self):
        from windows_mcp.desktop.input_timing import DoubleClickGuard

        now = [100.0]
        slept: list[float] = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        guard = DoubleClickGuard(metrics=lambda: (0.5, 4, 4), clock=lambda: now[0], sleep=sleep)
        guard.now = now
        guard.slept = slept
        return guard

    def test_first_click_never_waits(self, guard):
        assert guard.before_click(10, 10) == 0.0

    def test_second_click_at_same_spot_waits_out_the_double_click_time(self, guard):
        guard.after_click(10, 10)
        guard.now[0] += 0.1
        waited = guard.before_click(11, 9)
        assert waited == pytest.approx(0.4 + 0.015)
        assert guard.slept == [waited]

    def test_click_outside_the_double_click_rect_does_not_wait(self, guard):
        guard.after_click(10, 10)
        assert guard.before_click(13, 10) == 0.0

    def test_other_button_does_not_wait(self, guard):
        guard.after_click(10, 10, "left")
        assert guard.before_click(10, 10, "right") == 0.0

    def test_after_the_window_no_wait(self, guard):
        guard.after_click(10, 10)
        guard.now[0] += 0.6
        assert guard.before_click(10, 10) == 0.0

    def test_metrics_failure_does_not_block_input(self, guard):
        guard._metrics = MagicMock(side_effect=OSError)
        guard.after_click(10, 10)
        assert guard.before_click(10, 10) == 0.0


@pytest.mark.skipif(sys.platform != "win32", reason="Desktop imports pywin32")
class TestDesktopClickTiming:
    @pytest.fixture
    def desktop(self, monkeypatch):
        from windows_mcp.desktop import service

        owner = MagicMock()
        monkeypatch.setattr(service, "get_controller", lambda: owner)
        monkeypatch.setattr(
            service.Desktop, "_validate_screen_point", staticmethod(lambda x, y: None)
        )
        guard = MagicMock()
        monkeypatch.setattr(service, "click_guard", guard)
        monkeypatch.setattr(service, "wait_for_foreground_at", MagicMock(return_value=True))
        click = MagicMock()
        monkeypatch.setattr(service.uia, "Click", click)
        monkeypatch.setattr(service.uia, "SendKeys", MagicMock())
        desktop = service.Desktop.__new__(service.Desktop)
        desktop.click_mock = click
        desktop.guard = guard
        desktop.service = service
        return desktop

    def test_single_click_is_guarded_and_uses_the_settle(self, desktop):
        desktop.click((5, 6))
        desktop.guard.before_click.assert_called_once_with(5, 6, "left")
        desktop.guard.after_click.assert_called_once_with(5, 6, "left")
        desktop.click_mock.assert_called_once_with(5, 6)

    def test_double_click_presses_are_not_separated_by_the_guard(self, desktop, monkeypatch):
        monkeypatch.setattr(desktop.service.uia, "GetDoubleClickTime", lambda: 500)
        desktop.click((5, 6), clicks=2)
        desktop.guard.before_click.assert_called_once()
        first, last = desktop.click_mock.call_args_list
        assert first.kwargs["waitTime"] > 0
        assert last.kwargs["waitTime"] is None

    def test_type_waits_for_focus_instead_of_sleeping(self, desktop, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(desktop.service, "sleep", sleeps.append)
        monkeypatch.setenv(INPUT_SETTLE_ENV, "30")
        desktop.type((5, 6), "ab", clear=True)
        desktop.service.wait_for_foreground_at.assert_called_once_with(5, 6)
        assert sleeps == [pytest.approx(0.03)]
        desktop.guard.before_click.assert_called_once_with(5, 6, "left")
