"""Post-input settle delay shared by the raw mouse/keyboard helpers.

python-uiautomation slept a fixed ``OPERATION_WAIT_TIME`` (0.5 s) after every
injected click, key chord and wheel tick. Injected input is queued in order by the
raw input thread, so a later event can never overtake an earlier one; the long sleep
only made every action slow (a Click tool call cost ~550 ms). The default is now a
short settle that gives the target a moment to repaint before the next tool
reads the screen. ``WINDOWS_MCP_INPUT_SETTLE_MS=500`` restores the old timing.
"""

import logging
import math
import os

__all__ = [
    "DEFAULT_INPUT_SETTLE_MS",
    "INPUT_SETTLE_ENV",
    "LEGACY_INPUT_SETTLE_MS",
    "MAX_INPUT_SETTLE_MS",
    "input_settle_seconds",
]

logger = logging.getLogger(__name__)

INPUT_SETTLE_ENV = "WINDOWS_MCP_INPUT_SETTLE_MS"
DEFAULT_INPUT_SETTLE_MS = 30.0
LEGACY_INPUT_SETTLE_MS = 500.0
MAX_INPUT_SETTLE_MS = 5000.0

_warned_values: set[str] = set()


def input_settle_seconds() -> float:
    """Return the delay to sleep after an injected input event, in seconds.

    Read on every call (an environment lookup is far cheaper than the input it
    follows) so the value can be changed for a running process and in tests.

    Returns:
        ``WINDOWS_MCP_INPUT_SETTLE_MS`` converted to seconds and clamped to
        ``0``–``MAX_INPUT_SETTLE_MS``; the default when it is unset, empty or not
        a finite number.
    """
    raw = os.environ.get(INPUT_SETTLE_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_INPUT_SETTLE_MS / 1000.0
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        if raw not in _warned_values:
            _warned_values.add(raw)
            logger.warning(
                "Ignoring invalid %s=%r; using %g ms",
                INPUT_SETTLE_ENV,
                raw,
                DEFAULT_INPUT_SETTLE_MS,
            )
        return DEFAULT_INPUT_SETTLE_MS / 1000.0
    return min(value, MAX_INPUT_SETTLE_MS) / 1000.0
