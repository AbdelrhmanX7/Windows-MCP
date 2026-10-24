"""Bounds how many UI elements a single tree capture may collect.

Some applications expose enormous flat lists/grids through UI Automation
(e.g. an unfiltered inventory grid with thousands of rows). Walking and
serializing every row can stall the target application for minutes and can
produce a response too large for the calling client to handle. TreeElementBudget
gives Tree.get_state a cheap, dependency-free way to stop early once a
configurable element count is reached, while still returning a partial —
clearly marked as truncated — result instead of hanging or failing outright.

The same budget also carries an optional per-window scope (a wall-clock deadline
and an element cap) so a single slow or enormous window cannot consume the whole
capture: once the window's scope is spent, traversal of that window stops and the
remaining windows are still walked (#383).
"""

from __future__ import annotations

import logging
import os
from time import perf_counter
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_TREE_ELEMENTS = 500
DEFAULT_WINDOW_TIME_BUDGET_SEC = 10.0

WINDOW_CUT_TIME = "time"
WINDOW_CUT_ELEMENTS = "elements"


def resolve_max_tree_elements(env: "os._Environ[str] | dict[str, str] | None" = None) -> int:
    """Read the tree-capture element limit from the environment.

    Args:
        env: Mapping to read from. Defaults to ``os.environ``.

    Returns:
        The configured limit, or ``DEFAULT_MAX_TREE_ELEMENTS`` if the
        variable is unset, blank, non-numeric, or not a positive integer.
    """
    source = os.environ if env is None else env
    raw = source.get("WINDOWS_MCP_MAX_TREE_ELEMENTS", "")
    if not raw.strip():
        return DEFAULT_MAX_TREE_ELEMENTS
    try:
        parsed = int(raw.strip())
    except ValueError:
        logger.warning(
            "Invalid WINDOWS_MCP_MAX_TREE_ELEMENTS value %r, using default %d",
            raw,
            DEFAULT_MAX_TREE_ELEMENTS,
        )
        return DEFAULT_MAX_TREE_ELEMENTS
    if parsed < 1:
        logger.warning(
            "WINDOWS_MCP_MAX_TREE_ELEMENTS %r must be >= 1, using default %d",
            parsed,
            DEFAULT_MAX_TREE_ELEMENTS,
        )
        return DEFAULT_MAX_TREE_ELEMENTS
    return parsed


def resolve_window_time_budget(
    env: "os._Environ[str] | dict[str, str] | None" = None,
) -> Optional[float]:
    """Read the per-window traversal time budget from the environment.

    Args:
        env: Mapping to read from. Defaults to ``os.environ``.

    Returns:
        Seconds a single window's tree walk may take, or None when the budget is
        disabled (``WINDOWS_MCP_WINDOW_TIME_BUDGET_SEC=0``). Unset, blank,
        non-numeric or negative values use ``DEFAULT_WINDOW_TIME_BUDGET_SEC``.
    """
    source = os.environ if env is None else env
    raw = source.get("WINDOWS_MCP_WINDOW_TIME_BUDGET_SEC", "")
    if not raw.strip():
        return DEFAULT_WINDOW_TIME_BUDGET_SEC
    try:
        parsed = float(raw.strip())
    except ValueError:
        parsed = -1.0
    if parsed != parsed or parsed < 0 or parsed == float("inf"):
        logger.warning(
            "Invalid WINDOWS_MCP_WINDOW_TIME_BUDGET_SEC value %r, using default %s",
            raw,
            DEFAULT_WINDOW_TIME_BUDGET_SEC,
        )
        return DEFAULT_WINDOW_TIME_BUDGET_SEC
    if parsed == 0:
        return None
    return parsed


def resolve_max_window_elements(
    env: "os._Environ[str] | dict[str, str] | None" = None,
) -> Optional[int]:
    """Read the per-window element cap from the environment.

    Args:
        env: Mapping to read from. Defaults to ``os.environ``.

    Returns:
        The most elements a single window may contribute, or None (no per-window
        cap, only the global ``WINDOWS_MCP_MAX_TREE_ELEMENTS`` limit applies) when
        ``WINDOWS_MCP_MAX_WINDOW_ELEMENTS`` is unset, blank, ``0``, or invalid.
    """
    source = os.environ if env is None else env
    raw = source.get("WINDOWS_MCP_MAX_WINDOW_ELEMENTS", "")
    if not raw.strip():
        return None
    try:
        parsed = int(raw.strip())
    except ValueError:
        logger.warning("Invalid WINDOWS_MCP_MAX_WINDOW_ELEMENTS value %r, ignoring it", raw)
        return None
    if parsed < 0:
        logger.warning("WINDOWS_MCP_MAX_WINDOW_ELEMENTS %r must be >= 0, ignoring it", parsed)
        return None
    return parsed or None


class TreeElementBudget:
    """Tracks how many output-affecting elements a tree traversal has captured.

    Besides the capture-wide ``limit``, a window scope opened with
    ``begin_window`` adds a per-window deadline and element cap. While a window
    scope is active, ``exhausted`` and ``try_consume`` honour all three, so every
    existing check in the traversal stops a window that runs over its own scope.
    A window cut does not set ``truncated``, which reports the capture-wide limit;
    ``end_window`` returns why the window was cut instead.
    """

    def __init__(
        self,
        limit: int = DEFAULT_MAX_TREE_ELEMENTS,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.limit = max(1, limit)
        self.count = 0
        self.truncated = False
        self._clock = clock
        self._window_active = False
        self._window_start_count = 0
        self._window_max_elements: Optional[int] = None
        self._window_deadline: Optional[float] = None
        self._window_cut_reason: Optional[str] = None

    def begin_window(
        self, max_elements: Optional[int] = None, max_seconds: Optional[float] = None
    ) -> None:
        """Open a per-window scope.

        Args:
            max_elements: Most elements this window may contribute, or None.
            max_seconds: Wall-clock seconds this window's walk may take, or None.
        """
        self._window_active = True
        self._window_start_count = self.count
        self._window_max_elements = max_elements if max_elements and max_elements > 0 else None
        self._window_deadline = (
            self._clock() + max_seconds if max_seconds and max_seconds > 0 else None
        )
        self._window_cut_reason = None

    def end_window(self) -> Optional[str]:
        """Close the per-window scope.

        Returns:
            ``WINDOW_CUT_TIME`` or ``WINDOW_CUT_ELEMENTS`` when the window was cut
            short by its own scope, otherwise None.
        """
        reason = self._window_cut_reason
        self._window_active = False
        self._window_max_elements = None
        self._window_deadline = None
        self._window_cut_reason = None
        return reason

    @property
    def global_exhausted(self) -> bool:
        """True once the capture-wide limit is reached, ignoring any window scope."""
        return self.count >= self.limit

    @property
    def window_remaining(self) -> Optional[int]:
        """Elements the current window may still add, or None without a window cap."""
        if not self._window_active or self._window_max_elements is None:
            return None
        used = self.count - self._window_start_count
        return max(0, self._window_max_elements - used)

    @property
    def window_exhausted(self) -> bool:
        """True once the active window scope's deadline or element cap is spent."""
        if not self._window_active:
            return False
        if self._window_cut_reason is not None:
            return True
        window_remaining = self.window_remaining
        if window_remaining is not None and window_remaining <= 0:
            self._window_cut_reason = WINDOW_CUT_ELEMENTS
            return True
        if self._window_deadline is not None and self._clock() >= self._window_deadline:
            self._window_cut_reason = WINDOW_CUT_TIME
            return True
        return False

    @property
    def exhausted(self) -> bool:
        return self.global_exhausted or self.window_exhausted

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.count)

    def try_consume(self, amount: int = 1) -> bool:
        """Register `amount` newly captured elements.

        Returns False (and marks the budget truncated) once the limit is
        reached; the caller should then stop appending/recursing further.
        `count` is hard-capped at `limit` — a single over-large `amount`
        cannot push it past the configured budget. Inside a window scope it is
        also capped at the window's element cap, and once the window's deadline
        or cap is spent this returns False without marking the capture truncated.
        """
        if amount <= 0:
            return not self.exhausted
        if self.global_exhausted:
            self.truncated = True
            return False
        if self.window_exhausted:
            return False
        if amount > self.remaining:
            self.count = self.limit
            self.truncated = True
            return False
        window_remaining = self.window_remaining
        if window_remaining is not None and amount > window_remaining:
            self.count += window_remaining
            self._window_cut_reason = WINDOW_CUT_ELEMENTS
            return False
        self.count += amount
        if self.global_exhausted:
            self.truncated = True
        return True
