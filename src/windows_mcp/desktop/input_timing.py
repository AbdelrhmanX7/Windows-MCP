"""Condition-based waits that replace the fixed half-second sleeps around input.

Two of the old 0.5 s sleeps did real work, by accident or on purpose:

* After every click, the 0.5 s wait was about as long as the system double-click
  time, so two separate clicks at the same spot never merged into a double-click
  (which selects a word that a following ``Type`` would then overwrite). With a
  short settle that has to be guaranteed explicitly: :class:`DoubleClickGuard`
  sleeps only when the next click would otherwise merge with the previous one.
* Before typing, the wait let the clicked window take the foreground. Input is
  queued in order, so the keys already follow the click; the only real hazard is a
  window that is still activating. :func:`wait_for_foreground_at` polls for that
  instead of sleeping blindly.
"""

import ctypes
import logging
import threading
import time

import win32con
import win32gui

__all__ = [
    "DoubleClickGuard",
    "click_guard",
    "wait_for_foreground_at",
]

logger = logging.getLogger(__name__)

_SM_CXDOUBLECLK = 36
_SM_CYDOUBLECLK = 37
# Margin past the double-click time so the system's own comparison cannot round
# the gap back inside the window.
_DOUBLE_CLICK_MARGIN_S = 0.015
FOREGROUND_WAIT_S = 0.2
_FOREGROUND_POLL_S = 0.01


def _double_click_metrics() -> tuple[float, int, int]:
    """Return (double-click time in seconds, rect width, rect height)."""
    user32 = ctypes.windll.user32
    return (
        user32.GetDoubleClickTime() / 1000.0,
        user32.GetSystemMetrics(_SM_CXDOUBLECLK),
        user32.GetSystemMetrics(_SM_CYDOUBLECLK),
    )


class DoubleClickGuard:
    """Keep consecutive single clicks from merging into a double-click.

    Windows reports a double-click when a second press of the same button lands
    inside the ``SM_CXDOUBLECLK`` x ``SM_CYDOUBLECLK`` rectangle centred on the first
    press, within ``GetDoubleClickTime()``. :meth:`before_click` sleeps out the rest
    of that window only in that case, so clicks elsewhere, or after a pause, cost
    nothing.
    """

    def __init__(self, metrics=_double_click_metrics, clock=time.monotonic, sleep=time.sleep):
        """Create a guard.

        Args:
            metrics: Callable returning (double-click seconds, rect width, rect height).
            clock: Monotonic clock in seconds.
            sleep: Sleep function, injectable for tests.
        """
        self._metrics = metrics
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last: tuple[str, int, int, float] | None = None

    def before_click(self, x: int, y: int, button: str = "left") -> float:
        """Wait, if needed, so a click at (x, y) is not read as a double-click.

        Args:
            x: Screen x of the coming click.
            y: Screen y of the coming click.
            button: ``"left"``, ``"right"`` or ``"middle"``.

        Returns:
            The seconds slept (0.0 when no wait was needed).
        """
        with self._lock:
            last = self._last
        if last is None or last[0] != button:
            return 0.0
        try:
            window, width, height = self._metrics()
        except Exception:
            logger.debug("Double-click metrics unavailable", exc_info=True)
            return 0.0
        _, lx, ly, at = last
        if abs(x - lx) > width // 2 or abs(y - ly) > height // 2:
            return 0.0
        remaining = at + window + _DOUBLE_CLICK_MARGIN_S - self._clock()
        if remaining <= 0:
            return 0.0
        self._sleep(remaining)
        return remaining

    def after_click(self, x: int, y: int, button: str = "left") -> None:
        """Record a click that was just sent.

        Args:
            x: Screen x of the click.
            y: Screen y of the click.
            button: ``"left"``, ``"right"`` or ``"middle"``.
        """
        with self._lock:
            self._last = (button, x, y, self._clock())

    def reset(self) -> None:
        """Forget the last click."""
        with self._lock:
            self._last = None


click_guard = DoubleClickGuard()


def wait_for_foreground_at(x: int, y: int, timeout: float = FOREGROUND_WAIT_S) -> bool:
    """Poll until the top-level window under (x, y) is the foreground window.

    Called after the click that focuses a text field and before the keys go out.
    Returns at once in the usual case where the window is already in front, and
    gives up after ``timeout`` for targets that never become the foreground window
    themselves (menus, owned popups, the desktop).

    Args:
        x: Screen x of the click.
        y: Screen y of the click.
        timeout: Maximum seconds to wait.

    Returns:
        True when the window under the point is in the foreground.
    """
    try:
        hwnd = win32gui.WindowFromPoint((x, y))
        root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT) if hwnd else 0
    except Exception:
        logger.debug("WindowFromPoint failed", exc_info=True)
        return False
    if not root:
        return False
    deadline = time.monotonic() + timeout
    while True:
        try:
            if win32gui.GetForegroundWindow() == root:
                return True
        except Exception:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(_FOREGROUND_POLL_S)
