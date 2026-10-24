"""Elevation state of the current process token (no third-party imports)."""

import ctypes
import ctypes.wintypes
import logging
import sys
from functools import lru_cache

__all__ = ["integrity_level", "is_admin", "uac_enabled"]

logger = logging.getLogger(__name__)

_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25
_TOKEN_ELEVATION = 20

# Mandatory label RIDs (SECURITY_MANDATORY_*_RID).
_INTEGRITY_NAMES = (
    (0x4000, "system"),
    (0x3000, "high"),
    (0x2000, "medium"),
    (0x1000, "low"),
    (0x0000, "untrusted"),
)


def _query_token(info_class: int, size: int = 64) -> ctypes.Array | None:
    """Return the raw ``GetTokenInformation`` buffer for this process, or None."""
    if sys.platform != "win32":
        return None
    HANDLE, DWORD = ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD
    # Private DLL objects so these prototypes never clash with ctypes.windll users.
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # GetCurrentProcess returns the (HANDLE)-1 pseudo-handle; without explicit
    # prototypes ctypes passes it as a C int and overflows.
    kernel32.GetCurrentProcess.restype = HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.CloseHandle.argtypes = [HANDLE]
    advapi32.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
    advapi32.OpenProcessToken.restype = ctypes.wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)
    ]
    advapi32.GetTokenInformation.restype = ctypes.wintypes.BOOL
    token = HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        return None
    try:
        buf = ctypes.create_string_buffer(size)
        needed = DWORD()
        if not advapi32.GetTokenInformation(
            token, info_class, buf, size, ctypes.byref(needed)
        ):
            return None
        return buf
    finally:
        kernel32.CloseHandle(token)


def is_admin() -> bool:
    """Return True when this process holds an enabled Administrators group.

    That is the case for an elevated process under UAC, and for every process of an
    administrator when UAC is off. A process started with a filtered or restricted
    token (``runas /trustlevel``) is not an admin even though its user is.
    """
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except AttributeError, OSError:
        return False


def integrity_level() -> str:
    """Return this process's mandatory integrity level: system/high/medium/low/untrusted.

    Returns ``"unknown"`` when the token cannot be read (not Windows, API failure).
    """
    try:
        buf = _query_token(_TOKEN_INTEGRITY_LEVEL)
        if buf is None:
            return "unknown"
        advapi32 = ctypes.WinDLL("advapi32")
        advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.wintypes.DWORD)
        # TOKEN_MANDATORY_LABEL starts with SID_AND_ATTRIBUTES whose first field is the
        # SID pointer.
        sid = ctypes.c_void_p.from_buffer(buf).value
        count = advapi32.GetSidSubAuthorityCount(ctypes.c_void_p(sid))[0]
        rid = advapi32.GetSidSubAuthority(ctypes.c_void_p(sid), count - 1)[0]
    except Exception:
        logger.debug("Could not read the token integrity level", exc_info=True)
        return "unknown"
    for floor, name in _INTEGRITY_NAMES:
        if rid >= floor:
            return name
    return "unknown"


@lru_cache(maxsize=1)
def uac_enabled() -> bool | None:
    """Return whether UAC (``EnableLUA``) is on, or None when it cannot be read."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "EnableLUA")
            return bool(value)
    except OSError, ImportError:
        return None
