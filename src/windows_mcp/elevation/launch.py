"""Start processes elevated: ShellExecuteEx "runas" and Task Scheduler (no third-party imports).

``runas`` shows one UAC consent dialog. That dialog runs on the secure desktop, which
no automation (this server included) can click. A scheduled task registered with
``RunLevel Highest`` starts elevated with no dialog at all, so it is preferred whenever
it is installed (``windows-mcp install-elevated``).
"""

import ctypes
import ctypes.wintypes
import logging
import os
import subprocess
import sys

__all__ = [
    "ERROR_CANCELLED",
    "ElevationError",
    "ElevatedProcess",
    "run_task",
    "shell_execute_runas",
    "task_exists",
]

logger = logging.getLogger(__name__)

ERROR_CANCELLED = 1223  # The user declined the UAC consent dialog.

_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SEE_MASK_NOASYNC = 0x00000100
_SEE_MASK_FLAG_NO_UI = 0x00000400
_SW_HIDE = 0
_SW_SHOWNORMAL = 1
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF
_CREATE_NO_WINDOW = 0x08000000


class ElevationError(Exception):
    """An elevated process could not be started."""


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.wintypes.HWND),
        ("lpVerb", ctypes.wintypes.LPCWSTR),
        ("lpFile", ctypes.wintypes.LPCWSTR),
        ("lpParameters", ctypes.wintypes.LPCWSTR),
        ("lpDirectory", ctypes.wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.wintypes.LPCWSTR),
        ("hkeyClass", ctypes.wintypes.HKEY),
        ("dwHotKey", ctypes.wintypes.DWORD),
        ("hIconOrMonitor", ctypes.wintypes.HANDLE),
        ("hProcess", ctypes.wintypes.HANDLE),
    ]


class ElevatedProcess:
    """Handle to a process started with ``runas``; only waiting and exit codes work on it.

    Args:
        handle: Process handle from ``ShellExecuteExW``.
    """

    def __init__(self, handle: int):
        self._handle = handle
        kernel32 = ctypes.windll.kernel32
        kernel32.GetProcessId.restype = ctypes.wintypes.DWORD
        self.pid = int(kernel32.GetProcessId(ctypes.wintypes.HANDLE(handle)))

    def poll(self) -> int | None:
        """Return the exit code, or None while the process is running."""
        if self._handle is None:
            return 0
        if ctypes.windll.kernel32.WaitForSingleObject(ctypes.wintypes.HANDLE(self._handle), 0):
            return None
        return self._exit_code()

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the process to exit; return its exit code, or None on timeout."""
        if self._handle is None:
            return 0
        ms = _INFINITE if timeout is None else max(0, int(timeout * 1000))
        if (
            ctypes.windll.kernel32.WaitForSingleObject(ctypes.wintypes.HANDLE(self._handle), ms)
            != _WAIT_OBJECT_0
        ):
            return None
        return self._exit_code()

    def _exit_code(self) -> int:
        code = ctypes.wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(
            ctypes.wintypes.HANDLE(self._handle), ctypes.byref(code)
        )
        return int(code.value)

    def close(self) -> None:
        """Release the process handle (the process keeps running)."""
        handle, self._handle = self._handle, None
        if handle is not None:
            ctypes.windll.kernel32.CloseHandle(ctypes.wintypes.HANDLE(handle))


def shell_execute_runas(
    file: str, parameters: list[str], cwd: str | None = None, show: bool = False
) -> ElevatedProcess:
    """Start ``file`` elevated through the UAC consent dialog.

    Blocks until the user answers the dialog (immediately when the caller is already
    elevated or consent is not required).

    Args:
        file: Executable to start.
        parameters: Its arguments, quoted with Windows rules.
        cwd: Working directory, or None for the current one.
        show: Show the process's window instead of starting it hidden.

    Returns:
        The started process.

    Raises:
        ElevationError: When the user declined consent or the start failed.
    """
    if sys.platform != "win32":
        raise ElevationError("elevation requires Windows")
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC | _SEE_MASK_FLAG_NO_UI
    info.lpVerb = "runas"
    info.lpFile = file
    info.lpParameters = subprocess.list2cmdline(parameters)
    info.lpDirectory = cwd or os.getcwd()
    info.nShow = _SW_SHOWNORMAL if show else _SW_HIDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            raise ElevationError("the UAC consent dialog was declined")
        raise ElevationError(f"ShellExecuteEx runas failed: {ctypes.FormatError(error)}")
    if not info.hProcess:
        raise ElevationError("ShellExecuteEx runas returned no process")
    return ElevatedProcess(info.hProcess)


def _schtasks(*args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )


def task_exists(name: str) -> bool:
    """Return whether a scheduled task called ``name`` is registered."""
    try:
        return _schtasks("/Query", "/TN", name).returncode == 0
    except OSError, subprocess.SubprocessError:
        return False


def run_task(name: str) -> None:
    """Start the scheduled task ``name`` now.

    Raises:
        ElevationError: When Task Scheduler refused.
    """
    try:
        result = _schtasks("/Run", "/TN", name)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ElevationError(f"schtasks /Run failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ElevationError(f"schtasks /Run {name} failed: {detail}")
