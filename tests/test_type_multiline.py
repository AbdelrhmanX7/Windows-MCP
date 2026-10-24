"""Type sends long lines as pastes and line breaks/tabs as real keys.

A multi-line text used to go key by key (~60 ms per character), so a short email draft took
about 17 s on the VM. The keys the target receives for Enter and Tab must not change.
"""

import pytest

from windows_mcp.desktop import service
from windows_mcp.desktop.service import Desktop

SINGLE_MONITOR = (0, 0, 1920, 1080)


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> tuple[Desktop, list[tuple[str, str]]]:
    instance = Desktop.__new__(Desktop)
    instance.desktop_state = None
    sent: list[tuple[str, str]] = []
    clipboard = {"text": "user clipboard"}

    def set_clip(text: str) -> None:
        clipboard["text"] = text

    def send_keys(keys: str, *args: object, **kwargs: object) -> None:
        if keys == "{Ctrl}v":
            sent.append(("paste", clipboard["text"]))
        else:
            sent.append(("key", keys))

    def noop(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(service, "sleep", lambda seconds: None)
    monkeypatch.setattr(service, "wait_for_foreground_at", noop, raising=False)
    monkeypatch.setattr(service.uia, "GetVirtualScreenRect", lambda: SINGLE_MONITOR)
    monkeypatch.setattr(service.uia, "Click", noop)
    monkeypatch.setattr(service.uia, "SendKeys", send_keys)
    monkeypatch.setattr(service.uia, "GetClipboardText", lambda: clipboard["text"])
    monkeypatch.setattr(service.uia, "SetClipboardText", set_clip)
    instance._clipboard = clipboard
    return instance, sent


def test_multiline_text_pastes_each_long_line_and_presses_enter_between(desktop) -> None:
    d, sent = desktop
    d.type(
        loc=(100, 100), text="To: team@example.com\nSubject: Grocery run tonight\n\nThanks, Adiutor"
    )
    assert sent == [
        ("paste", "To: team@example.com"),
        ("key", "{Enter}"),
        ("paste", "Subject: Grocery run tonight"),
        ("key", "{Enter}"),
        ("key", "{Enter}"),
        ("key", "T"),
        ("key", "h"),
        ("key", "a"),
        ("key", "n"),
        ("key", "k"),
        ("key", "s"),
        ("key", ","),
        ("key", " "),
        ("key", "A"),
        ("key", "d"),
        ("key", "i"),
        ("key", "u"),
        ("key", "t"),
        ("key", "o"),
        ("key", "r"),
    ]
    assert d._clipboard["text"] == "user clipboard"  # restored after the pastes


def test_crlf_and_tab_become_keys(desktop) -> None:
    d, sent = desktop
    d.type(loc=(100, 100), text="a\r\nb\tc")
    assert sent == [("key", "a"), ("key", "{Enter}"), ("key", "b"), ("key", "{Tab}"), ("key", "c")]


def test_long_line_with_braces_is_pasted_literally(desktop) -> None:
    d, sent = desktop
    d.type(loc=(100, 100), text='{"name": "milk", "qty": 2}', press_enter=True)
    assert sent == [("paste", '{"name": "milk", "qty": 2}'), ("key", "{Enter}")]


def test_short_text_still_goes_key_by_key_with_escaped_braces(desktop) -> None:
    d, sent = desktop
    d.type(loc=(100, 100), text="a{b}")
    assert sent == [("key", "a"), ("key", "{{}"), ("key", "b"), ("key", "{}}")]
