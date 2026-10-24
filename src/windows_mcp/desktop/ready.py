"""Return from an action when the UI is ready, and let ``Wait`` skip the guesswork.

Agents used to follow every Type/Click/Shortcut/App call with ``Wait(1-3)`` "just in
case": in one measured run 27 of 35 s of tool time were such sleeps. This module makes
the action itself wait (bounded, polling every 30 ms) until what it started has finished,
and says so in the tool result, so the next ``Wait`` is unnecessary; and it lets
``Wait`` return after 0.1 s when nothing is in flight.

Signals, all cheap enough to read after every action:

* Foreground window handle and title (Win32, microseconds). A dialog such as Notepad's
  "Save As" takes the foreground about 90 ms after Ctrl+S.
* Chromium browsers (Edge, Chrome, Brave, ...): the toolbar's ``ReloadButton``. Its
  FullDescription (Edge) or Name (Chrome) reads "Stop ..." from about 20 ms after a
  navigation starts until the page's load event, including the pre-commit gap where the
  old document is still shown and nothing else changes (3 s for a cold todomvc.com load
  on the test VM). The element is found once per window (10-35 ms) and cached; a read is
  about 0.5 ms. Web content itself is not queried while it loads (polling the page's
  tree slowed the load).
* The global cursor: the hourglass (``IDC_WAIT``) counts as busy.

Nothing is waited for when nothing changed: an action that leaves the foreground window,
its title and the browser's load state as they were returns after a single sample, so a
loop of ``Type(press_enter=True)`` into an in-page field pays about a millisecond each.

``WINDOWS_MCP_READY_TIMEOUT`` bounds every wait (seconds, default 3); ``0`` turns the
feature off (actions return at once, ``Wait`` sleeps the full duration).

UIA calls must run on the UIA worker thread (``infrastructure.thread_affinity``): the
tools that use this module are registered with ``run_sync_in_thread=False`` or route each
probe through ``run_uia_affine``.
"""

from __future__ import annotations

import ctypes
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "DEFAULT_READY_TIMEOUT_S",
    "READY_TIMEOUT_ENV",
    "Readiness",
    "Sample",
    "Settled",
    "WAIT_MIN_S",
    "WaitTracker",
    "readiness",
    "ready_timeout",
]

logger = logging.getLogger(__name__)

READY_TIMEOUT_ENV = "WINDOWS_MCP_READY_TIMEOUT"
DEFAULT_READY_TIMEOUT_S = 3.0
MAX_READY_TIMEOUT_S = 30.0
POLL_S = 0.02
# A change counts as finished once the window, title and load state stayed the same this long.
STABLE_S = 0.03
# Once a navigation has committed (the window title changed to the new page), this is how
# long the load event may still take before the page counts as shown: search result pages
# keep the "Stop" state for seconds while ads and images load, long after they are usable.
POST_COMMIT_S = 0.6
WAIT_MIN_S = 0.1
WAIT_IDLE_S = 0.1

# UIA ids, inlined so the module needs no wrapper singleton at import time.
_TREE_SCOPE_DESCENDANTS = 4
_PROP_NAME = 30005
_PROP_CLASS_NAME = 30012
_PROP_FULL_DESCRIPTION = 30159
_RELOAD_CLASS = "ReloadButton"
# Seconds a window without a reload button is remembered as such (Firefox, app windows).
_NEGATIVE_CACHE_S = 5.0
_IDC_WAIT = 32514
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"))
_CHROMIUM = {"msedge.exe", "chrome.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe"}

_warned_values: set[str] = set()


def ready_timeout() -> float:
    """Seconds an action may wait for the UI to settle; 0 disables readiness waits.

    Read on every call so a running server (and tests) can change it.

    Returns:
        ``WINDOWS_MCP_READY_TIMEOUT`` clamped to ``0``-``MAX_READY_TIMEOUT_S``; the default
        when unset, empty or invalid.
    """
    raw = os.environ.get(READY_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_READY_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        if raw not in _warned_values:
            _warned_values.add(raw)
            logger.warning(
                "Ignoring invalid %s=%r; using %g s",
                READY_TIMEOUT_ENV,
                raw,
                DEFAULT_READY_TIMEOUT_S,
            )
        return DEFAULT_READY_TIMEOUT_S
    return min(value, MAX_READY_TIMEOUT_S)


@dataclass(frozen=True)
class Sample:
    """One reading of the UI state."""

    hwnd: int = 0
    title: str = ""
    browser: bool = False
    loading: bool = False

    def same_window(self, other: "Sample") -> bool:
        return self.hwnd == other.hwnd and self.title == other.title


@dataclass(frozen=True)
class Settled:
    """What an action changed and how long it took to settle."""

    note: str
    elapsed: float
    state: str  # "loaded" | "shown" | "window" | "title" | "ready" | "loading" | "changing"


def _clean(title: str) -> str:
    return title.translate(_INVISIBLE).strip()


def _short(title: str, limit: int = 90) -> str:
    title = _clean(title)
    return title if len(title) <= limit else title[: limit - 3] + "..."


class UiProbe:
    """Reads :class:`Sample`s from the live desktop. Call only on the UIA worker thread."""

    def __init__(self) -> None:
        self._client = None
        # hwnd -> (reload button element or None, time it was looked up)
        self._reload: dict[int, tuple[object | None, float]] = {}
        self._idle_signature: dict[int, str] = {}
        self._reload_cond = None
        self._wait_cursor: int | None = None

    def _uia(self):
        if self._client is None:
            from windows_mcp.uia.core import _AutomationClient

            self._client = _AutomationClient.instance().IUIAutomation
        return self._client

    def _reload_button(self, hwnd: int) -> object | None:
        cached = self._reload.get(hwnd)
        now = time.monotonic()
        if cached is not None and (cached[0] is not None or now - cached[1] < _NEGATIVE_CACHE_S):
            return cached[0]
        client = self._uia()
        if self._reload_cond is None:
            self._reload_cond = client.CreatePropertyCondition(_PROP_CLASS_NAME, _RELOAD_CLASS)
        element = None
        try:
            root = client.ElementFromHandle(hwnd)
            if root:
                element = root.FindFirst(_TREE_SCOPE_DESCENDANTS, self._reload_cond) or None
        except Exception:
            logger.debug("Reload button lookup failed", exc_info=True)
        if len(self._reload) > 64:
            self._reload.clear()
            self._idle_signature.clear()
        self._reload[hwnd] = (element, now)
        return element

    def browser_loading(self, hwnd: int, deep: bool = False) -> bool:
        """Is the browser window's active tab loading?"""
        for attempt in range(2):
            button = self._reload_button(hwnd)
            if button is None:
                break
            try:
                name = button.GetCurrentPropertyValue(_PROP_NAME) or ""
                desc = button.GetCurrentPropertyValue(_PROP_FULL_DESCRIPTION) or ""
            except Exception:
                self._reload.pop(hwnd, None)
                if attempt:
                    break
                continue
            signature = f"{name}|{desc}"
            if str(name).lower().startswith("stop") or str(desc).lower().startswith("stop"):
                return True
            # Not English: anything other than the idle state seen first counts as loading.
            idle = self._idle_signature.setdefault(hwnd, signature)
            if signature != idle:
                return True
            break
        # Web-content UIA queries (the document's aria busy) are not polled: walking a
        # loading page's tree every 20 ms slowed the load itself on the test VM.
        return False

    def _cursor_busy(self) -> bool:
        class CURSORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("flags", ctypes.c_uint),
                ("hCursor", ctypes.c_void_p),
                ("x", ctypes.c_long),
                ("y", ctypes.c_long),
            ]

        try:
            user32 = ctypes.windll.user32
            if self._wait_cursor is None:
                user32.LoadCursorW.restype = ctypes.c_void_p
                user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                self._wait_cursor = user32.LoadCursorW(None, ctypes.c_void_p(_IDC_WAIT)) or 0
            info = CURSORINFO()
            info.cbSize = ctypes.sizeof(CURSORINFO)
            if not user32.GetCursorInfo(ctypes.byref(info)):
                return False
            return bool(info.hCursor) and info.hCursor == self._wait_cursor
        except Exception:
            return False

    def sample(self, deep: bool = False) -> Sample:
        from windows_mcp.guard.context import foreground, process_name

        hwnd, title, pid = foreground()
        browser = process_name(pid) in _CHROMIUM if hwnd else False
        loading = False
        if browser:
            try:
                loading = self.browser_loading(hwnd, deep=deep)
            except Exception:
                logger.debug("Browser load probe failed", exc_info=True)
        if not loading:
            loading = self._cursor_busy()
        return Sample(hwnd=hwnd, title=_clean(title), browser=browser, loading=loading)


class Readiness:
    """Bounded settle-after-action and early-returning waits over a sample source."""

    def __init__(
        self,
        sample: Callable[[bool], Sample] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout: Callable[[], float] = ready_timeout,
    ) -> None:
        self._probe = None if sample is not None else UiProbe()
        self._sample = sample
        self._clock = clock
        self._sleep = sleep
        self._timeout = timeout

    def enabled(self) -> bool:
        return self._timeout() > 0

    def sample(self, deep: bool = False) -> Sample:
        """Read the UI state; never raises (an unreadable desktop is an idle one)."""
        try:
            if self._sample is not None:
                return self._sample(deep)
            return self._probe.sample(deep)
        except Exception:
            logger.debug("Readiness sample failed", exc_info=True)
            return Sample()

    def before(self) -> Sample | None:
        """Sample taken right before an action, or None when readiness is off."""
        if not self.enabled():
            return None
        return self.sample()

    def after(
        self,
        before: Sample | None,
        *,
        watch: float = 0.0,
        launched: bool = False,
        timeout: float | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> Settled | None:
        """Wait until what the action started has finished; None when nothing changed.

        Args:
            before: :meth:`before` sample from just before the action (None: readiness off).
            watch: Seconds to keep looking for a change to start (0 = one sample).
            launched: The action opened a window (already found and focused by the caller):
                describe it without waiting for a browser's start page to load.
            timeout: Override for the bounded wait (defaults to ``WINDOWS_MCP_READY_TIMEOUT``).
            checkpoint: Called on every poll; may raise to abort (user took control).

        Returns:
            A :class:`Settled` describing the change, or None when the UI did not change.
        """
        limit = self._timeout() if timeout is None else min(timeout, self._timeout())
        if before is None or limit <= 0:
            return None
        start = self._clock()
        deadline = start + limit
        watch_end = start + watch
        stable_for = 0.0 if launched else STABLE_S
        cur = self.sample()
        # A load already running before the action (a start page, a slow tab) was not
        # started by it: window shortcuts such as win+up must not wait for it.
        ignore_loading = launched or before.loading
        busy = cur.loading and not ignore_loading
        changed = launched or busy or not cur.same_window(before)
        saw_loading = busy
        last, last_change = cur, start
        committed_at = start if busy and cur.title != before.title else None
        while True:
            now = self._clock()
            if not changed and now >= watch_end:
                return None
            if changed and not busy and now - last_change >= stable_for:
                return self._describe(before, cur, saw_loading, now - start, launched)
            if busy and committed_at is not None and now - committed_at >= POST_COMMIT_S:
                elapsed = now - start
                return Settled(
                    f'Page shown: "{_short(cur.title)}" ({elapsed:.1f} s; images/ads may '
                    "still be loading).",
                    elapsed,
                    "shown",
                )
            if now >= deadline:
                return self._describe(before, cur, saw_loading, now - start, launched, busy)
            if checkpoint is not None:
                checkpoint()
            self._sleep(min(POLL_S, max(0.0, deadline - now)))
            cur = self.sample(changed and cur.browser and not launched)
            busy = cur.loading and not ignore_loading
            if busy:
                saw_loading = True
                if committed_at is None and cur.browser and cur.title != before.title:
                    committed_at = self._clock()
            if not changed:
                if busy or not cur.same_window(before):
                    changed = True
                    last, last_change = cur, self._clock()
                continue
            if busy or not cur.same_window(last):
                last, last_change = cur, self._clock()

    @staticmethod
    def _describe(
        before: Sample,
        cur: Sample,
        saw_loading: bool,
        elapsed: float,
        launched: bool,
        busy: bool | None = False,
    ) -> Settled:
        title = _short(cur.title) or "(untitled)"
        if busy:
            if cur.browser:
                return Settled(
                    f'Page still loading after {elapsed:.1f} s: "{title}".', elapsed, "loading"
                )
            return Settled(f'UI still busy after {elapsed:.1f} s: "{title}".', elapsed, "loading")
        if launched:
            return Settled(f'Window "{title}" is ready ({elapsed:.1f} s).', elapsed, "ready")
        if saw_loading and cur.browser:
            return Settled(f'Page loaded: "{title}" ({elapsed:.1f} s).', elapsed, "loaded")
        if cur.hwnd != before.hwnd:
            return Settled(
                f'Window "{title}" is now in front ({elapsed:.1f} s).', elapsed, "window"
            )
        if cur.title != before.title:
            return Settled(f'Window title is now "{title}" ({elapsed:.1f} s).', elapsed, "title")
        return Settled(f"UI settled ({elapsed:.1f} s).", elapsed, "changing")

    def wait(self, duration: float) -> tuple[float, str]:
        """Sleep up to ``duration`` but return once the UI is idle (blocking variant).

        Returns:
            (seconds waited, message).
        """
        tracker = WaitTracker(self._clock(), duration)
        deep = False
        while True:
            result = tracker.update(self.sample(deep), self._clock())
            if result is not None:
                return result
            deep = tracker.busy and tracker.last.browser
            self._sleep(tracker.sleep_for(self._clock()))


class WaitTracker:
    """State of one early-returning ``Wait``: feed it samples, it says when to stop.

    Pure logic (no I/O), shared by the blocking :meth:`Readiness.wait` and the async
    ``Wait`` tool, which reads each sample on the UIA worker and sleeps on the event loop.
    The wait returns once at least ``WAIT_MIN_S`` passed and the UI was idle (nothing
    loading, same foreground window and title) for ``WAIT_IDLE_S``; while something is
    loading or changing it keeps waiting, up to the requested duration.
    """

    def __init__(self, start: float, duration: float) -> None:
        self.start = start
        self.duration = max(0.0, duration)
        self.deadline = start + self.duration
        self.min_end = start + min(WAIT_MIN_S, self.duration)
        self.first: Sample | None = None
        self.last = Sample()
        self.last_change = start
        self.busy = False
        self.activity = False

    def update(self, cur: Sample, now: float) -> tuple[float, str] | None:
        """Record a sample; return (seconds waited, message) when the wait is over."""
        if self.first is None:
            self.first = cur
            self.activity = cur.loading
        elif cur.loading or not cur.same_window(self.last):
            self.activity = True
            self.last_change = now
        self.last = cur
        self.busy = cur.loading
        waited = now - self.start
        if not self.busy and now >= self.min_end and now - self.last_change >= WAIT_IDLE_S:
            return waited, self._message(waited, full=False)
        if now >= self.deadline:
            return waited, self._message(waited, full=True)
        return None

    def sleep_for(self, now: float) -> float:
        return min(POLL_S, max(0.0, self.deadline - now))

    def _message(self, waited: float, full: bool) -> str:
        title = _short(self.last.title) or "(no window)"
        if full and self.busy:
            what = "page still loading" if self.last.browser else "UI still busy"
            return f'Waited {waited:.1f} s; {what}: "{title}".'
        if self.activity:
            what = "page finished loading" if self.last.browser else "UI settled"
            return f'Waited {waited:.1f} s until the {what}: "{title}".'
        if full:
            return f'Waited {waited:.1f} s. Foreground: "{title}".'
        return (
            f"UI already idle: returned after {waited:.1f} s of the requested "
            f'{self.duration:g} s (foreground "{title}", nothing loading). Actions already '
            "wait for readiness, so Wait is not needed after them."
        )


readiness = Readiness()
