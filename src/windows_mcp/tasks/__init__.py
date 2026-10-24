"""Task orchestration: plans of dependent tasks, live events, a screen lock and RunParallel.

Pure Python (no Windows imports), so it is unit-testable on any platform.
"""

from windows_mcp.tasks.events import TaskEventBus
from windows_mcp.tasks.runtime import OrchestrationMiddleware, TaskRuntime, get_runtime
from windows_mcp.tasks.screen_lock import ScreenLock, ScreenLockTimeout, get_screen_lock
from windows_mcp.tasks.store import Plan, Task, TaskError, TaskStore

__all__ = [
    "OrchestrationMiddleware",
    "Plan",
    "ScreenLock",
    "ScreenLockTimeout",
    "Task",
    "TaskError",
    "TaskEventBus",
    "TaskRuntime",
    "TaskStore",
    "get_runtime",
    "get_screen_lock",
]
