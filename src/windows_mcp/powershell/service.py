""" PowerShell command executor service """

import atexit
import base64
import ctypes
import ctypes.wintypes
import logging
import os
import winreg
import shutil
import subprocess
import threading

from windows_mcp.desktop.utils import is_elevated
from windows_mcp.powershell.session import PowerShellSession, SessionResult, SessionUnavailable
from windows_mcp.powershell.utils import run_with_graceful_timeout

__all__ = ["PowerShellExecutor", "prewarm_session", "session_enabled", "shutdown_sessions"]

logger = logging.getLogger(__name__)


def _read_reg_env(hkey: int, subkey: str) -> tuple[dict[str, str], str, str]:
    """Read all environment variables from a registry key.

    Returns (vars, path, pathext) where *vars* is a dict of name->value
    (excluding PATH/PATHEXT), and *path*/*pathext* are the raw expanded
    values for those two special keys (empty string if absent).
    """
    variables: dict[str, str] = {}
    path = ""
    pathext = ""

    try:
        with winreg.OpenKey(hkey, subkey) as key:
            i = 0
            while True:
                try:
                    name, value, reg_type = winreg.EnumValue(key, i)
                    if reg_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                        i += 1
                        continue
                    if reg_type == winreg.REG_EXPAND_SZ:
                        # Note: ExpandEnvironmentStrings uses the current process
                        # environment, which may be stripped by the MCP host. References
                        # to missing vars (e.g. %COMPUTERNAME%) won't expand correctly.
                        # In practice this is acceptable: most REG_EXPAND_SZ values under
                        # HKLM/HKCU\Environment reference %SystemRoot% or %USERPROFILE%
                        # which are almost always present.
                        value = winreg.ExpandEnvironmentStrings(value)
                    upper = name.upper()
                    if upper == "PATH":
                        path = value
                    elif upper == "PATHEXT":
                        pathext = value
                    else:
                        variables[name] = value
                    i += 1
                except OSError:
                    break
    except OSError:
        pass

    return variables, path, pathext


def _dedup_path(*segments: str) -> str:
    """Join PATH segments and deduplicate entries (case-insensitive)."""
    seen = set()
    deduped = []
    for p in ";".join(filter(None, segments)).split(";"):
        norm = p.lower().rstrip("\\")
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(p)
    return ";".join(deduped)


def _win32_name(dll: str, func: str) -> str:
    """Call a Win32 GetXxxNameW(LPWSTR, LPDWORD) function.

    Only suitable for APIs with the (buffer, &size) calling convention,
    e.g. kernel32.GetComputerNameW and advapi32.GetUserNameW.
    """
    buf = ctypes.create_unicode_buffer(257)
    size = ctypes.wintypes.DWORD(257)
    fn = getattr(ctypes.windll, dll)
    if getattr(fn, func)(buf, ctypes.byref(size)):
        return buf.value
    return ""


_FALLBACK_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.CPL;.PY;.PYW"


def _prepare_env() -> dict[str, str]:
    """Prepare a complete environment block for the PowerShell subprocess.

    MCP hosts (e.g. Claude Desktop) may launch this server with a stripped
    environment block missing session-level variables. This function starts
    from os.environ and fills in missing variables from:
      1. System-level env vars from the registry (HKLM)
      2. User-level env vars from the registry (HKCU)
      3. Dynamic vars (COMPUTERNAME, USERNAME, etc.) via Win32 API
    Existing values in os.environ are never overwritten, only missing ones
    are supplemented. PATH is special-cased: inherited entries keep their
    priority and registry entries are appended to fill in missing paths.
    """
    env = os.environ.copy()

    # PYTHONHOME is set by uv to pin its own managed interpreter for this
    # server process. Leaking it into a spawned shell makes any Python found
    # on PATH there resolve its standard library against the server's
    # interpreter instead of its own, breaking unrelated installs (#350).
    env.pop("PYTHONHOME", None)

    try:
        machine_vars, machine_path, machine_pathext = _read_reg_env(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        )
        user_vars, user_path, user_pathext = _read_reg_env(winreg.HKEY_CURRENT_USER, r"Environment")

        # Supplement missing vars. User-level (HKCU) takes precedence over
        # system-level (HKLM) for same-named keys, matching Windows' resolution order.
        for name, value in {**machine_vars, **user_vars}.items():
            if not env.get(name):
                env[name] = value

        # PATH: inherited entries keep their priority; registry entries are
        # appended to fill in anything missing (e.g. stripped MCP host env).
        if machine_path or user_path:
            env["PATH"] = _dedup_path(env.get("PATH", ""), machine_path, user_path)

        # PATHEXT: use registry value if the inherited one looks incomplete
        effective_pathext = user_pathext or machine_pathext
        if effective_pathext and ".EXE" not in env.get("PATHEXT", "").upper():
            env["PATHEXT"] = effective_pathext

    except Exception:
        logger.debug("Failed to read environment from registry", exc_info=True)
        if ".EXE" not in env.get("PATHEXT", "").upper():
            env["PATHEXT"] = _FALLBACK_PATHEXT

    # Dynamic variables not stored in registry — only fill if missing
    if not env.get("COMPUTERNAME"):
        try:
            computer_name = _win32_name("kernel32", "GetComputerNameW")
            if computer_name:
                env["COMPUTERNAME"] = computer_name
        except Exception as e:
            logger.debug("Failed to get COMPUTERNAME via Win32 API: %s", e)

    if not env.get("USERNAME"):
        try:
            user_name = _win32_name("advapi32", "GetUserNameW")
            if user_name:
                env["USERNAME"] = user_name
        except Exception as e:
            logger.debug("Failed to get USERNAME via Win32 API: %s", e)

    user_profile = os.path.expanduser("~")
    env.setdefault("USERPROFILE", user_profile)
    drive, tail = os.path.splitdrive(user_profile)
    env.setdefault("HOMEDRIVE", drive)
    env.setdefault("HOMEPATH", tail)
    # On domain-joined machines USERDOMAIN is the NetBIOS domain name (e.g.
    # "CORP"), not the computer name. This fallback is only correct for workgroup
    # machines. USERDOMAIN is a session variable set at logon and is unlikely to
    # be missing on domain-joined hosts, so this is an acceptable last resort.
    env.setdefault("USERDOMAIN", env.get("COMPUTERNAME", ""))

    return env


_WINDOWS_POWERSHELL = os.path.join("System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def _resolve_shell(shell: str | None, env: dict[str, str]) -> str:
    """Resolve the PowerShell executable to launch against the child's PATH.

    Windows looks a bare program name up on the *server's* PATH, not on the
    environment handed to the child, so the PATH repaired by ``_prepare_env()``
    does not decide which executable is found. ``System32\\WindowsPowerShell\\v1.0``
    is not on the default search list either, so when the MCP host strips the
    server's PATH a bare ``powershell`` fails with WinError 2 (#411).

    Args:
        shell: The shell the caller asked for, or ``None`` to prefer ``pwsh`` and
            fall back to Windows PowerShell.
        env: The environment the child will run with.

    Returns:
        An absolute path when the shell can be found; otherwise Windows
        PowerShell's well-known location for a PowerShell request, or the name
        unchanged for any other shell so the launch error names it.
    """
    search_path = env.get("PATH")
    for name in (shell,) if shell else ("pwsh", "powershell"):
        found = shutil.which(name, path=search_path)
        if found:
            return found

    if shell and os.path.basename(shell).lower().removesuffix(".exe") != "powershell":
        return shell
    system_root = env.get("SystemRoot") or os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(system_root, _WINDOWS_POWERSHELL)


SESSION_ENV = "WINDOWS_MCP_POWERSHELL_SESSION"
_SESSION_OFF_VALUES = ("0", "false", "no", "off", "disabled")
# Consecutive failed starts of one shell before the session is given up on for
# the rest of the process lifetime (every call then uses the one-shot path).
_MAX_SESSION_START_FAILURES = 3

_sessions: dict[str, PowerShellSession] = {}
_session_start_failures: dict[str, int] = {}
_sessions_lock = threading.Lock()


def session_enabled() -> bool:
    """Return whether commands may run in the persistent session.

    On unless ``WINDOWS_MCP_POWERSHELL_SESSION`` is ``0``/``false``/``no``/``off``.
    """
    return os.environ.get(SESSION_ENV, "on").strip().lower() not in _SESSION_OFF_VALUES


def shutdown_sessions() -> None:
    """Stop every persistent session and clear the start-failure counts."""
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
        _session_start_failures.clear()
    for session in sessions:
        try:
            session.close()
        except Exception:
            logger.debug("Failed to close PowerShell session", exc_info=True)


atexit.register(shutdown_sessions)


def _session_for(shell: str, env: dict[str, str], cwd: str) -> PowerShellSession | None:
    """Return the session for ``shell``, replacing one that died or went stale."""
    stale = None
    with _sessions_lock:
        if _session_start_failures.get(shell, 0) >= _MAX_SESSION_START_FAILURES:
            return None
        session = _sessions.get(shell)
        if session is not None and not session.lock.locked():
            # A session is replaced when its host died (timeout, exit, crash) or
            # when the environment a fresh shell would get has changed, e.g. PATH
            # updated in the registry by an installer.
            started = session.pid is not None
            if (started and not session.alive) or session.env != env:
                stale, session = session, None
                del _sessions[shell]
        if session is None:
            session = PowerShellSession(shell, env, cwd)
            _sessions[shell] = session
    if stale is not None:
        stale.close()
    return session


def _run_in_session(
    shell: str, env: dict[str, str], command: str, cwd: str, timeout: float
) -> SessionResult | None:
    """Run ``command`` in the persistent session for ``shell``.

    Returns:
        The result, or None when the command was not delivered (session disabled,
        busy with another call, or failed to start) and must run one-shot instead.
    """
    session = _session_for(shell, env, cwd)
    if session is None:
        return None
    # Never queue behind another call (a 30 s command would stall App launch):
    # a busy session sends this call down the one-shot path, as before.
    if not session.lock.acquire(blocking=False):
        return None
    try:
        if session.pid is None:
            try:
                session.start()
            except SessionUnavailable as exc:
                with _sessions_lock:
                    _session_start_failures[shell] = _session_start_failures.get(shell, 0) + 1
                    if _sessions.get(shell) is session:
                        del _sessions[shell]
                logger.debug("PowerShell session unavailable, using one-shot: %s", exc)
                return None
            with _sessions_lock:
                _session_start_failures.pop(shell, None)
        try:
            return session.run(command, cwd, timeout)
        except SessionUnavailable as exc:
            logger.debug("PowerShell session lost before the command, using one-shot: %s", exc)
            return None
    finally:
        session.lock.release()


def prewarm_session(shell: str | None = None) -> bool:
    """Start the persistent session ahead of the first command.

    Args:
        shell: The shell to warm, as accepted by ``execute_command``.

    Returns:
        True when a session is running afterwards.
    """
    if not session_enabled():
        return False
    env = _prepare_env()
    env["NO_COLOR"] = "1"
    resolved = _resolve_shell(shell, env)
    result = _run_in_session(resolved, env, "$null", os.path.expanduser("~"), 30)
    return result is not None


def _one_shot(
    shell: str, env: dict[str, str], command: str, cwd: str, timeout: float
) -> tuple[str | bytes, str | bytes, int]:
    """Run ``command`` in a fresh PowerShell process (the pre-session behaviour).

    Raises:
        subprocess.TimeoutExpired: If the command overran ``timeout``.
    """
    # $OutputEncoding: controls how PS5.1 encodes output written to its stdout pipe.
    # Without this set to UTF-8, PS5.1 uses the system codepage and native process
    # stdout is silently lost when Python reads the pipe.
    # [Console]::OutputEncoding: controls how PS decodes bytes from native exe stdout.
    utf8_command = (
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        f"{command}"
    )
    encoded = base64.b64encode(utf8_command.encode("utf-16le")).decode("ascii")
    args = [shell, "-NoProfile"]
    # Only older Windows PowerShell (5.1) uses -OutputFormat Text successfully here
    shell_name = os.path.basename(shell).lower().replace(".exe", "")
    if shell_name == "powershell":
        args.extend(["-OutputFormat", "Text"])
    args.extend(["-EncodedCommand", encoded])

    result = run_with_graceful_timeout(
        args,
        stdin=subprocess.DEVNULL,  # Prevent child processes from inheriting the MCP pipe stdin
        capture_output=True,  # No errors='ignore' - let subprocess return bytes
        timeout=timeout,
        cwd=cwd,
        env=env,
    )
    return result.stdout, result.stderr, result.returncode


class PowerShellExecutor:
    """Static utility class for executing PowerShell commands.

    Commands run in a persistent PowerShell session (see ``powershell/session.py``)
    unless ``WINDOWS_MCP_POWERSHELL_SESSION`` is off; a command the session cannot
    take (busy, failed to start) runs in a fresh process instead.
    """

    @staticmethod
    def execute_command(
            command: str, timeout: int = 10, shell: str | None = None
    ) -> tuple[str, int]:
        try:
            env = _prepare_env()
            # NO_COLOR suppresses ANSI escape sequences in pwsh 7.2+ (and many other CLI tools).
            # PS5.1 has no ANSI output, so this is harmlessly ignored there.
            # https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_ansi_terminals#disabling-ansi-output
            env["NO_COLOR"] = "1"

            shell = _resolve_shell(shell, env)
            cwd = os.path.expanduser(path="~")

            session_result = None
            if session_enabled():
                session_result = _run_in_session(shell, env, command, cwd, timeout)
            if session_result is not None:
                if session_result.timed_out:
                    return "Command execution timed out", 1
                stdout, stderr = session_result.stdout, session_result.stderr
                returncode = session_result.returncode
            else:
                stdout, stderr, returncode = _one_shot(shell, env, command, cwd, timeout)
            # Handle both bytes and str output (subprocess behavior varies by environment)
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            output = stdout or stderr
            # If the command failed with "Access is denied" and we aren't elevated, add a helpful hint
            if returncode != 0 and "Access is denied" in output and not is_elevated():
                output += (
                    "\n\nHINT: This command may require an elevated (Administrator) terminal. "
                    "The Windows-MCP server is currently running at a lower integrity level."
                )
            return output, returncode
        except subprocess.TimeoutExpired:
            return "Command execution timed out", 1
        except Exception as e:
            # WinError 2 does not say which file was missing, so name the shell.
            logger.warning("PowerShell command failed to run (shell=%s)", shell, exc_info=True)
            return f"Command execution failed: {type(e).__name__}: {e} (shell: {shell})", 1
