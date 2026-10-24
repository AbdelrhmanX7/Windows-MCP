"""A long-lived PowerShell host that runs commands without paying process startup.

Starting ``powershell.exe`` costs about a second, which used to dominate every
PowerShell, Registry and App call. :class:`PowerShellSession` starts the shell once
and feeds it commands over a private pipe.

Protocol:

* The host is started with stdin set to ``NUL`` (as the one-shot path is), so
  commands that read stdin (``Read-Host``, ``cmd /c pause``, ``python``) see EOF
  instead of swallowing protocol traffic. Commands arrive on a separate anonymous
  pipe whose read handle is the only extra handle the host inherits.
* Each request is one line: ``<nonce> <base64 cwd> <base64 command>``. The nonce is
  a fresh random hex string per call, so command output cannot forge the end
  marker.
* The host runs the command as a script block in a child scope and then writes
  ``\\n<<WMCP-END-<nonce>:<status>>>\\n`` to stdout and ``\\n<<WMCP-END-<nonce>>>\\n``
  to stderr. Everything before the markers is the command's output.

Per-command state, matching a fresh ``powershell -Command`` as closely as a shared
process allows:

* Reset before each command: the location (and process working directory), the
  process environment block (variables added, changed or removed by an earlier
  command are restored to the session's start-up values), ``$LASTEXITCODE``,
  ``$Error``, and the UTF-8 console encodings.
* Not reset: anything a command puts in the global scope on purpose
  (``$global:x``, ``Set-Variable -Scope Global``), imported modules, and types
  loaded with ``Add-Type``. Plain variables and functions live in the command's
  child scope and disappear with it.
* ``exit N`` ends the host: the call returns the output so far with status ``N``
  and the next call starts a new host.

The status is 1 when the command's last statement failed (``$?`` false) or threw,
otherwise 0, as with ``powershell -Command``.
"""

import base64
import logging
import msvcrt
import os
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass

import psutil

__all__ = [
    "PowerShellSession",
    "SessionResult",
    "SessionUnavailable",
]

logger = logging.getLogger(__name__)

_READY = b"<<WMCP-READY>>\n"
_STDERR_GRACE_S = 2.0
_STOP_WAIT_S = 2.0

# The host loop. ``__HANDLE__`` is replaced with the command pipe's handle value.
# Its variables are prefixed so commands are unlikely to collide with them.
_HOST_SCRIPT = r"""
$__wmcpUtf8 = New-Object System.Text.UTF8Encoding $false
$__wmcpHandle = New-Object Microsoft.Win32.SafeHandles.SafeFileHandle([IntPtr]__HANDLE__, $true)
$__wmcpPipe = New-Object System.IO.FileStream($__wmcpHandle, [System.IO.FileAccess]::Read, 4096, $false)
$__wmcpIn = New-Object System.IO.StreamReader($__wmcpPipe, $__wmcpUtf8)
$__wmcpEnv = @{}
foreach ($__wmcpE in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
    $__wmcpEnv[[string]$__wmcpE.Key] = [string]$__wmcpE.Value
}
$OutputEncoding = $__wmcpUtf8
[Console]::OutputEncoding = $__wmcpUtf8
[Console]::Out.Write("<<WMCP-READY>>`n")
while ($true) {
    $__wmcpLine = $__wmcpIn.ReadLine()
    if ($null -eq $__wmcpLine) { break }
    $__wmcpParts = $__wmcpLine.Split(' ')
    $__wmcpId = $__wmcpParts[0]
    $__wmcpCwd = $__wmcpUtf8.GetString([Convert]::FromBase64String($__wmcpParts[1]))
    $__wmcpCode = $__wmcpUtf8.GetString([Convert]::FromBase64String($__wmcpParts[2]))
    foreach ($__wmcpK in @([Environment]::GetEnvironmentVariables().Keys)) {
        if (-not $__wmcpEnv.ContainsKey([string]$__wmcpK)) {
            [Environment]::SetEnvironmentVariable([string]$__wmcpK, $null)
        }
    }
    foreach ($__wmcpK in $__wmcpEnv.Keys) {
        if ([Environment]::GetEnvironmentVariable($__wmcpK) -cne $__wmcpEnv[$__wmcpK]) {
            [Environment]::SetEnvironmentVariable($__wmcpK, $__wmcpEnv[$__wmcpK])
        }
    }
    $OutputEncoding = $__wmcpUtf8
    if ([Console]::OutputEncoding.WebName -ne 'utf-8') { [Console]::OutputEncoding = $__wmcpUtf8 }
    Set-Location -LiteralPath $__wmcpCwd
    [Environment]::CurrentDirectory = $__wmcpCwd
    $global:LASTEXITCODE = 0
    $global:Error.Clear()
    $global:__wmcpOk = $true
    $__wmcpStatus = 0
    $__wmcpParseErrors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseInput(
        $__wmcpCode, [ref]$null, [ref]$__wmcpParseErrors)
    try {
        if ($__wmcpParseErrors.Count -gt 0) {
            [Console]::Error.WriteLine((($__wmcpParseErrors | ForEach-Object { $_.ToString() }) -join "`n"))
            $__wmcpStatus = 1
        } else {
            # The trailer records $? of the command's last statement, which is what
            # decides a `powershell -Command` exit code.
            $__wmcpBlock = [ScriptBlock]::Create($__wmcpCode + "`n`$global:__wmcpOk = `$?")
            # The one-pass loop catches a top-level break/continue in the command,
            # which would otherwise unwind into this loop.
            do { & $__wmcpBlock | Out-Default } while ($false)
            if (-not $global:__wmcpOk) { $__wmcpStatus = 1 }
        }
    } catch {
        [Console]::Error.WriteLine(($_ | Out-String))
        $__wmcpStatus = 1
    }
    [Console]::Out.Write("`n<<WMCP-END-" + $__wmcpId + ":" + $__wmcpStatus + ">>`n")
    [Console]::Error.Write("`n<<WMCP-END-" + $__wmcpId + ">>`n")
}
"""


class SessionUnavailable(Exception):
    """The session could not take the command; nothing was delivered to it."""


@dataclass
class SessionResult:
    """Outcome of one command run in a session.

    Attributes:
        stdout: Decoded standard output of the command.
        stderr: Decoded standard error of the command.
        returncode: The command's status (see the module docstring).
        timed_out: True when the command overran its timeout and the host was killed.
    """

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


class _StreamCollector:
    """Drain a pipe on a daemon thread into a buffer that waiters can search."""

    def __init__(self, stream, name: str):
        self._stream = stream
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self.eof = False
        self._thread = threading.Thread(target=self._pump, name=name, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            while True:
                chunk = self._stream.read1(65536)
                if not chunk:
                    break
                with self._cond:
                    self._buffer += chunk
                    self._cond.notify_all()
        except OSError, ValueError:
            pass
        finally:
            with self._cond:
                self.eof = True
                self._cond.notify_all()

    def clear(self) -> None:
        """Drop anything buffered (stray output written between commands)."""
        with self._cond:
            del self._buffer[:]

    def wait_for(self, pattern: re.Pattern[bytes], deadline: float) -> re.Match[bytes] | None:
        """Block until ``pattern`` appears, the stream ends, or ``deadline`` passes.

        Args:
            pattern: Compiled bytes pattern to search the buffer for.
            deadline: ``time.monotonic()`` value to give up at.

        Returns:
            The match, or None on EOF or timeout.
        """
        with self._cond:
            while True:
                match = pattern.search(self._buffer)
                if match is not None:
                    return match
                remaining = deadline - time.monotonic()
                if self.eof or remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def wait_eof(self, deadline: float) -> bool:
        """Block until the stream ends or ``deadline`` passes; return whether it ended."""
        with self._cond:
            while not self.eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def take(self, end: int | None = None, cut: int | None = None) -> bytes:
        """Remove and return buffered bytes.

        Args:
            end: Return ``buffer[:end]`` (everything when None).
            cut: Remove ``buffer[:cut]`` (defaults to ``end``).

        Returns:
            The returned bytes.
        """
        with self._cond:
            if end is None:
                end = len(self._buffer)
            if cut is None:
                cut = end
            data = bytes(self._buffer[:end])
            del self._buffer[:cut]
            return data


def _decode(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return text[1:] if text.startswith("﻿") else text


class PowerShellSession:
    """One persistent PowerShell host process. Not thread-safe; callers serialize.

    Args:
        shell: Absolute path of the PowerShell executable.
        env: Environment block for the host (kept to detect when it goes stale).
        cwd: Working directory the host starts in.
    """

    def __init__(self, shell: str, env: dict[str, str], cwd: str):
        self.shell = shell
        self.env = dict(env)
        self.cwd = cwd
        self.lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._cmd = None
        self._out: _StreamCollector | None = None
        self._err: _StreamCollector | None = None

    @property
    def alive(self) -> bool:
        """True while the host process is running and its command pipe is open."""
        return self._proc is not None and self._cmd is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        """The host's process id, if started."""
        return self._proc.pid if self._proc is not None else None

    def build_args(self, handle: int) -> list[str]:
        """Return the command line that starts the host.

        Args:
            handle: Inheritable handle value of the command pipe's read end.

        Returns:
            The argv list for ``subprocess.Popen``.
        """
        script = _HOST_SCRIPT.replace("__HANDLE__", str(handle))
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        args = [self.shell, "-NoProfile", "-NoLogo"]
        if os.path.basename(self.shell).lower().removesuffix(".exe") == "powershell":
            args.extend(["-OutputFormat", "Text"])
        # Not -EncodedCommand: with it, Windows PowerShell 5.1 serializes the error
        # stream as CLIXML and only flushes it when the process exits, so a
        # long-lived host would never deliver a command's errors. -Command keeps
        # errors as plain text written as they happen.
        args.extend(
            [
                "-Command",
                f"iex ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')))",
            ]
        )
        return args

    def start(self, timeout: float = 20.0) -> None:
        """Start the host and wait until it reports ready.

        Args:
            timeout: Seconds to wait for the ready marker.

        Raises:
            SessionUnavailable: If the host could not be started.
        """
        read_fd, write_fd = os.pipe()
        try:
            read_handle = msvcrt.get_osfhandle(read_fd)
            os.set_handle_inheritable(read_handle, True)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.lpAttributeList = {"handle_list": [read_handle]}
            self._proc = subprocess.Popen(
                self.build_args(read_handle),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
                close_fds=True,
                startupinfo=startupinfo,
                # Same flags as the one-shot path: no console window flash, and
                # its own process group so a CTRL_BREAK never reaches the server.
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            os.close(write_fd)
            self._proc = None
            raise SessionUnavailable(f"failed to start {self.shell}: {exc}") from exc
        finally:
            os.close(read_fd)
        self._cmd = os.fdopen(write_fd, "wb", buffering=0)
        self._out = _StreamCollector(self._proc.stdout, f"pwsh-session-{self._proc.pid}-out")
        self._err = _StreamCollector(self._proc.stderr, f"pwsh-session-{self._proc.pid}-err")
        ready = self._out.wait_for(re.compile(re.escape(_READY)), time.monotonic() + timeout)
        if ready is None:
            detail = _decode(self._err.take())[-500:]
            self.close()
            raise SessionUnavailable(f"{self.shell} did not become ready: {detail.strip()}")
        self._out.take(cut=ready.end())
        self._err.clear()
        logger.debug("PowerShell session started: pid=%s shell=%s", self._proc.pid, self.shell)

    def run(self, command: str, cwd: str, timeout: float) -> SessionResult:
        """Run one command and return its output.

        Args:
            command: PowerShell source to run.
            cwd: Location to set before running it.
            timeout: Seconds before the host (and processes this command started)
                is killed.

        Returns:
            The command's result. After a timeout or an ``exit``, the session is
            dead and must be replaced.

        Raises:
            SessionUnavailable: If the command could not be delivered. It was not
                run, so the caller may run it some other way.
        """
        if not self.alive:
            raise SessionUnavailable("session is not running")
        nonce = secrets.token_hex(16)
        line = " ".join(
            (
                nonce,
                base64.b64encode(cwd.encode("utf-8")).decode("ascii"),
                base64.b64encode(command.encode("utf-8")).decode("ascii"),
            )
        )
        self._out.clear()
        self._err.clear()
        started = time.time()
        try:
            self._cmd.write(line.encode("ascii") + b"\n")
        except OSError as exc:
            self.close()
            raise SessionUnavailable(f"failed to send command: {exc}") from exc

        deadline = time.monotonic() + timeout
        out_end = re.compile(rb"\n<<WMCP-END-" + nonce.encode() + rb":(-?\d+)>>\n")
        err_end = re.compile(rb"\n<<WMCP-END-" + nonce.encode() + rb">>\n")
        out_match = self._out.wait_for(out_end, deadline)
        if out_match is None:
            if self._out.eof or self._proc.poll() is not None:
                return self._result_after_exit()
            logger.debug("PowerShell session command timed out after %ss", timeout)
            self._kill_since(started)
            stdout, stderr = _decode(self._out.take()), _decode(self._err.take())
            self.close()
            return SessionResult(stdout, stderr, 1, timed_out=True)
        # Read the group before take(): the match views the live buffer.
        returncode = int(out_match.group(1))
        stdout = _decode(self._out.take(out_match.start(), out_match.end()))
        err_match = self._err.wait_for(err_end, max(deadline, time.monotonic() + _STDERR_GRACE_S))
        if err_match is None:
            stderr = _decode(self._err.take())
        else:
            stderr = _decode(self._err.take(err_match.start(), err_match.end()))
        return SessionResult(stdout, stderr, returncode)

    def _result_after_exit(self) -> SessionResult:
        """Collect the output of a command that ended the host (``exit``, crash)."""
        try:
            returncode = self._proc.wait(timeout=_STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            returncode = 1
        # Let the pipe threads drain what the host wrote last.
        drain_deadline = time.monotonic() + 0.5
        self._out.wait_eof(drain_deadline)
        self._err.wait_eof(drain_deadline)
        stdout, stderr = _decode(self._out.take()), _decode(self._err.take())
        self.close()
        return SessionResult(stdout, stderr, returncode)

    def _kill_since(self, since: float) -> None:
        """Kill the host and the descendants it started at or after ``since``.

        A plain tree kill would also take down apps launched by earlier commands
        (``App`` launches through this host), so only processes created during the
        timed-out command are killed.
        """
        if self._proc is None:
            return
        try:
            children = psutil.Process(self._proc.pid).children(recursive=True)
        except psutil.Error:
            children = []
        for child in children:
            try:
                # `since` is read before the command is written, so nothing this
                # command started can predate it; older children are left alone.
                if child.create_time() >= since:
                    child.kill()
            except psutil.Error:
                pass
        try:
            self._proc.kill()
        except OSError:
            pass

    def close(self) -> None:
        """Stop the host. Processes it launched keep running."""
        cmd, self._cmd = self._cmd, None
        if cmd is not None:
            try:
                cmd.close()
            except OSError:
                pass
        proc = self._proc
        if proc is None:
            return
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=_STOP_WAIT_S)
            except OSError, subprocess.TimeoutExpired:
                pass
        # The reader threads are not joined: a surviving grandchild can keep the
        # pipes open, and the daemon threads end on their own at EOF.
