"""Register/remove the scheduled tasks that give admin mode its no-prompt start.

Two per-user tasks, both ``RunLevel Highest`` with an ``Interactive`` logon so they run
on the user's own desktop (where UIA can drive elevated windows):

* ``windows-mcp-server-elevated`` -- runs ``windows-mcp serve`` elevated. stdio is not
  usable from a scheduled task, so it serves ``streamable-http`` on ``127.0.0.1``.
* ``windows-mcp-elevated-worker`` -- runs ``windows_mcp.elevation.worker --rendezvous``
  so a non-elevated server's ``elevated=true`` calls get an admin worker with no prompt.

Registering a ``RunLevel Highest`` task itself needs elevation; ``install-elevated``
self-elevates once via ``runas`` to call :func:`install`. The tasks then start elevated
forever after with no further prompt.
"""

import logging
import subprocess
import sys

from windows_mcp.elevation.client import WORKER_TASK_NAME

__all__ = [
    "SERVER_TASK_NAME",
    "WORKER_TASK_NAME",
    "install",
    "task_status",
    "uninstall",
]

logger = logging.getLogger(__name__)

SERVER_TASK_NAME = "windows-mcp-server-elevated"
_CREATE_NO_WINDOW = 0x08000000


def _ps_single_quote(text: str) -> str:
    """Quote ``text`` as a PowerShell single-quoted string."""
    return "'" + text.replace("'", "''") + "'"


def _register_script(task_name: str, execute: str, argument: str) -> str:
    """Build the PowerShell that registers one Highest/Interactive task.

    Args:
        task_name: Scheduled-task name.
        execute: Program the action runs.
        argument: Its argument string.

    Returns:
        A single-line PowerShell command.
    """
    return (
        f"$ErrorActionPreference='Stop';"
        f"$action=New-ScheduledTaskAction -Execute {_ps_single_quote(execute)}"
        f" -Argument {_ps_single_quote(argument)};"
        # AtLogOn keeps the trigger valid for `schtasks /Run`; the task is started on
        # demand, not only at logon. A disabled trigger would make /Run a no-op.
        f"$trigger=New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;"
        f"$principal=New-ScheduledTaskPrincipal -UserId \"$env:USERDOMAIN\\$env:USERNAME\""
        f" -LogonType Interactive -RunLevel Highest;"
        f"$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries"
        f" -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)"
        f" -MultipleInstances IgnoreNew;"
        f"Register-ScheduledTask -TaskName {_ps_single_quote(task_name)} -Action $action"
        f" -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null"
    )


def _run_powershell(script: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )


def _schtasks(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )


def _program() -> list[str]:
    """The argv prefix that invokes this exact installation's CLI."""
    return [sys.executable, "-m", "windows_mcp"]


def install(
    host: str = "127.0.0.1",
    port: int = 8000,
    include_server: bool = True,
    include_worker: bool = True,
) -> list[str]:
    """Register the admin-mode scheduled tasks. Must run elevated.

    Args:
        host: Loopback address the elevated server binds.
        port: Port the elevated server binds.
        include_server: Register the elevated-server task.
        include_worker: Register the elevated-worker task.

    Returns:
        The names of the tasks that were registered.

    Raises:
        RuntimeError: When a registration failed.
    """
    program = subprocess.list2cmdline(_program())
    registered: list[str] = []
    jobs = []
    if include_server:
        # Stateful (legacy) streamable-http so the backend still receives pushed
        # task_event / security_event / control notifications over its session.
        server_args = (
            f"{program} serve --transport streamable-http --host {host} --port {port}"
        )
        jobs.append((SERVER_TASK_NAME, "cmd.exe", f'/c "{server_args}"'))
    if include_worker:
        worker_args = subprocess.list2cmdline(
            [sys.executable, "-m", "windows_mcp.elevation.worker", "--rendezvous"]
        )
        jobs.append((WORKER_TASK_NAME, "cmd.exe", f'/c "{worker_args}"'))

    for task_name, execute, argument in jobs:
        result = _run_powershell(_register_script(task_name, execute, argument))
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Register-ScheduledTask {task_name} failed: {detail}")
        registered.append(task_name)
    return registered


def uninstall() -> list[str]:
    """Remove both admin-mode tasks. Returns the names that were removed."""
    removed = []
    for task_name in (SERVER_TASK_NAME, WORKER_TASK_NAME):
        end = _schtasks("/End", "/TN", task_name)
        logger.debug("schtasks /End %s -> %s", task_name, end.returncode)
        if _schtasks("/Delete", "/TN", task_name, "/F").returncode == 0:
            removed.append(task_name)
    return removed


def task_status() -> dict[str, bool]:
    """Return whether each admin-mode task is currently registered."""
    return {
        name: _schtasks("/Query", "/TN", name).returncode == 0
        for name in (SERVER_TASK_NAME, WORKER_TASK_NAME)
    }
