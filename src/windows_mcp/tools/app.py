"""App tool — launch, resize, switch applications."""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from mcp.types import ToolAnnotations
from windows_mcp.desktop.ready import readiness, ready_timeout
from windows_mcp.infrastructure import with_analytics
from fastmcp import Context

logger = logging.getLogger(__name__)

# Seconds a launched window may take to hold its title still before App returns.
_LAUNCH_READY_S = 1.5
# Seconds launch_executable waits for the new process's window to appear.
_EXECUTABLE_WINDOW_S = 5.0


def _as_args(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        args = value
    else:
        args = json.loads(value)
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("args must be a list of strings")
    return args


def _resolve_executable(executable: str) -> Path:
    path = Path(executable).expanduser().resolve()
    if not path.is_file():
        # A bare name such as "notepad.exe" is looked up on PATH, as a shell would.
        found = shutil.which(executable) if not Path(executable).parent.parts else None
        if found is None:
            raise ValueError(f"Executable does not exist: {path}")
        path = Path(found).resolve()
    return path


def _resolve_cwd(cwd: str | None) -> Path | None:
    if cwd is None:
        return None
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Working directory does not exist: {path}")
    return path


def _launch_executable(
    executable: str,
    args: list[str] | str | None,
    cwd: str | None,
    desktop=None,
) -> str:
    resolved_executable = _resolve_executable(executable)
    resolved_cwd = _resolve_cwd(cwd)
    resolved_args = _as_args(args)
    windows_before = _top_level_windows(desktop)

    process = subprocess.Popen(
        [str(resolved_executable), *resolved_args],
        cwd=str(resolved_cwd) if resolved_cwd is not None else None,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    result = {
        "pid": process.pid,
        "executable": str(resolved_executable),
        "args": resolved_args,
        "cwd": str(resolved_cwd) if resolved_cwd is not None else None,
    }
    if windows_before is not None:
        result["window"] = _ready_window(
            desktop, resolved_executable.stem, process.pid, windows_before
        )
    return json.dumps(result, indent=2)


def _top_level_windows(desktop) -> set[int] | None:
    """Windows before a launch, or None when the launch should not wait for its window."""
    if desktop is None or ready_timeout() <= 0:
        return None
    try:
        return desktop._top_level_windows()
    except Exception:
        logger.debug("Could not list windows before launch", exc_info=True)
        return None


def _ready_window(desktop, name: str, pid: int, before: set[int]) -> str:
    """Wait for the launched process's window, bring it to the front and describe it."""
    try:
        hwnd = desktop._wait_for_launched_window(
            name, pid, before, timeout=min(_EXECUTABLE_WINDOW_S, ready_timeout())
        )
    except Exception:
        logger.debug("Waiting for the launched window failed", exc_info=True)
        hwnd = None
    if hwnd is None:
        return "not detected yet"
    try:
        import win32gui

        if win32gui.GetForegroundWindow() != hwnd:
            desktop.bring_window_to_top(hwnd)
    except Exception:
        logger.debug("Could not focus launched window %s", hwnd, exc_info=True)
    settled = readiness.after(readiness.before(), launched=True, timeout=_LAUNCH_READY_S)
    return settled.note if settled is not None else "opened"


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="App",
        description=(
            "Open/start/launch applications and manage windows. Keywords: open, start, launch, program, "
            "application, window, foreground, focus, resize. Four modes: 'launch' (opens an application "
            "by Start Menu name), 'launch_executable' (strictly launches one executable path with separated "
            "argv and optional cwd), 'resize' (adjusts a named or active window), and 'switch' (brings a "
            "specific window into focus). launch and launch_executable return once the new window is in front and ready (no Wait needed); launch_executable also accepts a bare name on PATH such as 'notepad.exe'. Prefer background execution when supported. If launching an application is known to take focus or disrupt the user, obtain permission first, reusing existing authorization. Relaxed policy does not gate launch modes; this guidance is not server-enforced approval."
        ),
        annotations=ToolAnnotations(
            title="App",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "App-Tool", run_sync_in_thread=False)
    def app_tool(
        mode: Literal["launch", "launch_executable", "resize", "switch"] = "launch",
        name: str | None = None,
        window_loc: list[int] | None = None,
        window_size: list[int] | None = None,
        executable: str | None = None,
        args: list[str] | str | None = None,
        cwd: str | None = None,
        ctx: Context = None,
    ):
        exact_launch_inputs = (executable, args, cwd)
        if mode != "launch_executable" and any(value is not None for value in exact_launch_inputs):
            raise ValueError('executable, args, and cwd require mode="launch_executable"')

        if mode == "launch_executable":
            if executable is None:
                raise ValueError('executable is required for mode="launch_executable"')
            if name is not None or window_loc is not None or window_size is not None:
                raise ValueError(
                    "name, window_loc, and window_size are not supported for "
                    'mode="launch_executable"'
                )
            return _launch_executable(executable, args, cwd, get_desktop())

        result = get_desktop().app(mode, name, window_loc, window_size)
        if mode == "launch" and isinstance(result, str) and result.endswith(" launched."):
            settled = readiness.after(readiness.before(), launched=True, timeout=_LAUNCH_READY_S)
            if settled is not None:
                result = f"{result} {settled.note}"
        return result
