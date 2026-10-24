"""App(mode="launch") finds the new window quickly and brings it to the front.

Edge's launcher process exits after handing off to a second msedge.exe, and its title holds
a zero-width space ("Microsoft\\u200b Edge"), so the old pid wait always ran its full 10 s and
the title search could miss. A window launched through the PowerShell session also stayed
behind the desktop, which hid it from Snapshot.
"""

import pytest

from windows_mcp.desktop import service
from windows_mcp.desktop.service import Desktop


class FakeWindows:
    def __init__(self) -> None:
        self.windows: dict[int, tuple[int, str]] = {
            1: (100, "Program Manager")
        }  # hwnd -> (pid, title)
        self.exe: dict[int, str] = {100: "explorer.exe"}
        self.foreground = 1
        self.focused: list[int] = []

    def enum(self, callback, extra) -> None:
        for hwnd in list(self.windows):
            if callback(hwnd, extra) is False:
                return


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeWindows:
    w = FakeWindows()
    monkeypatch.setattr(service, "sleep", lambda seconds: None)
    monkeypatch.setattr(service.win32gui, "EnumWindows", w.enum)
    monkeypatch.setattr(service.win32gui, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(service.win32gui, "GetWindow", lambda hwnd, flag: 0)
    monkeypatch.setattr(service.win32gui, "GetWindowText", lambda hwnd: w.windows[hwnd][1])
    monkeypatch.setattr(service.win32gui, "GetForegroundWindow", lambda: w.foreground)
    monkeypatch.setattr(
        service.win32process, "GetWindowThreadProcessId", lambda hwnd: (0, w.windows[hwnd][0])
    )

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            if pid not in w.exe:
                raise ProcessLookupError(pid)
            self.pid = pid

        def name(self) -> str:
            return w.exe[self.pid]

    monkeypatch.setattr(service, "Process", FakeProcess)
    return w


def _desktop(fake: FakeWindows, launch) -> Desktop:
    d = Desktop.__new__(Desktop)
    d.launch_app = launch
    d.bring_window_to_top = lambda hwnd: (
        fake.focused.append(hwnd),
        setattr(fake, "foreground", hwnd),
    )
    return d


def test_edge_hand_off_is_found_by_exe_and_brought_to_front(fake: FakeWindows) -> None:
    def launch(name):
        fake.exe[200] = "msedge.exe"  # launcher, exits without a window
        fake.exe[201] = "msedge.exe"
        fake.windows[7] = (201, "New tab - Profile 1 - Microsoft​ Edge")
        return "ok", 0, 200

    d = _desktop(fake, launch)
    assert d.app("launch", name="Microsoft Edge") == "Microsoft Edge launched."
    assert fake.focused == [7]


def test_title_match_ignores_zero_width_characters(fake: FakeWindows) -> None:
    def launch(name):
        fake.exe[300] = "ApplicationFrameHost.exe"
        fake.windows[8] = (301, "Sticky​ Notes")
        return "ok", 0, 0

    d = _desktop(fake, launch)
    assert d.app("launch", name="Sticky Notes") == "Sticky Notes launched."
    assert fake.focused == [8]


def test_window_already_in_front_is_not_refocused(fake: FakeWindows) -> None:
    def launch(name):
        fake.exe[400] = "notepad.exe"
        fake.windows[9] = (400, "Untitled - Notepad")
        fake.foreground = 9
        return "ok", 0, 400

    d = _desktop(fake, launch)
    assert d.app("launch", name="notepad") == "Notepad launched."
    assert fake.focused == []


def test_old_window_of_the_same_app_does_not_count(fake: FakeWindows, monkeypatch) -> None:
    fake.windows[5] = (500, "Untitled - Notepad")
    fake.exe[500] = "notepad.exe"
    clock = iter(range(0, 100))
    monkeypatch.setattr(service, "perf_counter", lambda: next(clock))

    def launch(name):
        return "ok", 0, 0

    d = _desktop(fake, launch)
    assert d.app("launch", name="notepad") == "Launching Notepad sent, but window not detected yet."
    assert fake.focused == []
