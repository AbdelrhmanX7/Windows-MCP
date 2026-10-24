"""Persistent PowerShell session: framing, timeout/crash recovery and one-shot fallback.

Most tests drive the session protocol with a small Python stand-in for the
PowerShell host (``FAKE_HOST``), so they are fast and deterministic; the
``TestRealPowerShell`` class checks the real host loop against Windows PowerShell.
"""

import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows pipes and PowerShell")

if sys.platform == "win32":
    import psutil

    from windows_mcp.powershell import service
    from windows_mcp.powershell.service import PowerShellExecutor
    from windows_mcp.powershell.session import (
        PowerShellSession,
        SessionUnavailable,
    )

# Speaks the session protocol. Each command is ";"-separated ops:
#   out:TEXT  err:TEXT  sleep:SECONDS  status:N  exit:N  cwd  stdin  pid  stray:TEXT
FAKE_HOST = textwrap.dedent(
    r"""
    import base64, msvcrt, os, sys, time
    cmd = os.fdopen(msvcrt.open_osfhandle(int(sys.argv[1]), os.O_RDONLY), "rb")
    out, err = sys.stdout.buffer, sys.stderr.buffer
    out.write(b"<<WMCP-READY>>\n"); out.flush()
    for raw in cmd:
        nonce, cwd, code = raw.decode("ascii").rstrip("\n").split(" ")
        cwd = base64.b64decode(cwd).decode()
        code = base64.b64decode(code).decode()
        status, stray = 0, None
        for stmt in filter(None, code.split(";")):
            op, _, arg = stmt.partition(":")
            if op == "out": out.write(arg.encode()); out.flush()
            elif op == "err": err.write(arg.encode()); err.flush()
            elif op == "sleep": time.sleep(float(arg))
            elif op == "status": status = int(arg)
            elif op == "exit": out.flush(); err.flush(); os._exit(int(arg))
            elif op == "cwd": out.write(cwd.encode()); out.flush()
            elif op == "stdin": out.write(repr(sys.stdin.read()).encode()); out.flush()
            elif op == "pid": out.write(str(os.getpid()).encode()); out.flush()
            elif op == "stray": stray = arg
        out.write(f"\n<<WMCP-END-{nonce}:{status}>>\n".encode()); out.flush()
        err.write(f"\n<<WMCP-END-{nonce}>>\n".encode()); err.flush()
        if stray:
            out.write(stray.encode()); out.flush()
    """
)


@pytest.fixture
def fake_host(tmp_path):
    path = tmp_path / "fake_host.py"
    path.write_text(FAKE_HOST)
    return str(path)


@pytest.fixture
def fake_session_cls(fake_host, monkeypatch):
    """A PowerShellSession that starts the fake host, installed in the service."""

    class FakeSession(PowerShellSession):
        starts = 0

        def build_args(self, handle):
            return [sys.executable, fake_host, str(handle)]

        def start(self, timeout=20.0):
            type(self).starts += 1
            super().start(timeout)

    monkeypatch.setattr(service, "PowerShellSession", FakeSession)
    monkeypatch.setattr(service, "_resolve_shell", lambda shell, env: shell or "fake-pwsh")
    monkeypatch.delenv(service.SESSION_ENV, raising=False)
    return FakeSession


@pytest.fixture
def session(fake_host, tmp_path):
    class FakeSession(PowerShellSession):
        def build_args(self, handle):
            return [sys.executable, fake_host, str(handle)]

    s = FakeSession("fake-pwsh", dict(os.environ), str(tmp_path))
    s.start()
    yield s
    s.close()


class TestFraming:
    def test_stdout_stderr_and_status_are_split_at_the_markers(self, session, tmp_path):
        result = session.run("out:hello;err:oops;status:3", str(tmp_path), 5)
        assert (result.stdout, result.stderr, result.returncode) == ("hello", "oops", 3)
        assert not result.timed_out

    def test_back_to_back_commands_do_not_bleed(self, session, tmp_path):
        first = session.run("out:one", str(tmp_path), 5)
        second = session.run("out:two", str(tmp_path), 5)
        assert (first.stdout, second.stdout) == ("one", "two")

    def test_output_that_looks_like_a_marker_is_kept(self, session, tmp_path):
        fake = "\n<<WMCP-END-0000:0>>\n"
        result = session.run(f"out:{fake}", str(tmp_path), 5)
        assert result.stdout == fake

    def test_unicode_round_trips(self, session, tmp_path):
        result = session.run("out:héllo 日本 🌍", str(tmp_path), 5)
        assert result.stdout == "héllo 日本 🌍"

    def test_cwd_is_sent_with_every_command(self, session, tmp_path):
        other = tmp_path / "ünï"
        assert session.run("cwd", str(other), 5).stdout == str(other)

    def test_stray_output_between_commands_is_dropped(self, session, tmp_path):
        session.run("stray:LATE", str(tmp_path), 5)
        time.sleep(0.2)
        assert session.run("out:next", str(tmp_path), 5).stdout == "next"

    def test_host_stdin_is_nul_not_the_command_pipe(self, session, tmp_path):
        assert session.run("stdin", str(tmp_path), 5).stdout == "''"

    def test_exit_returns_output_and_code_then_session_is_dead(self, session, tmp_path):
        result = session.run("out:bye;exit:7", str(tmp_path), 5)
        assert (result.stdout, result.returncode) == ("bye", 7)
        assert not session.alive
        with pytest.raises(SessionUnavailable):
            session.run("out:x", str(tmp_path), 5)

    def test_timeout_kills_the_host(self, session, tmp_path):
        pid = session.pid
        started = time.monotonic()
        result = session.run("out:partial;sleep:30", str(tmp_path), 1)
        assert time.monotonic() - started < 10
        assert result.timed_out and result.returncode == 1
        assert not session.alive
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE

    def test_start_failure_raises_session_unavailable(self, tmp_path):
        s = PowerShellSession(str(tmp_path / "missing.exe"), dict(os.environ), str(tmp_path))
        with pytest.raises(SessionUnavailable):
            s.start()


class TestExecutorWithSession:
    def test_reuses_one_host(self, fake_session_cls):
        first = PowerShellExecutor.execute_command("pid")
        second = PowerShellExecutor.execute_command("pid")
        assert first[1] == second[1] == 0
        assert first[0] == second[0]
        assert fake_session_cls.starts == 1

    def test_output_falls_back_to_stderr_like_one_shot(self, fake_session_cls):
        assert PowerShellExecutor.execute_command("err:boom;status:1") == ("boom", 1)

    def test_timeout_message_then_respawn(self, fake_session_cls):
        before = PowerShellExecutor.execute_command("pid")[0]
        assert PowerShellExecutor.execute_command("sleep:30", timeout=1) == (
            "Command execution timed out",
            1,
        )
        after, code = PowerShellExecutor.execute_command("pid")
        assert code == 0 and after != before
        assert fake_session_cls.starts == 2

    def test_crash_then_respawn(self, fake_session_cls):
        assert PowerShellExecutor.execute_command("out:x;exit:9") == ("x", 9)
        assert PowerShellExecutor.execute_command("out:again") == ("again", 0)
        assert fake_session_cls.starts == 2

    def test_environment_change_respawns(self, fake_session_cls, monkeypatch):
        PowerShellExecutor.execute_command("pid")
        monkeypatch.setenv("WMCP_TEST_ENV_CHANGE", "1")
        PowerShellExecutor.execute_command("pid")
        assert fake_session_cls.starts == 2

    def test_start_failure_falls_back_to_one_shot(self, fake_session_cls, monkeypatch):
        def broken_start(self, timeout=20.0):
            raise SessionUnavailable("nope")

        monkeypatch.setattr(fake_session_cls, "start", broken_start)
        one_shot = []
        monkeypatch.setattr(
            service,
            "_one_shot",
            lambda shell, env, command, cwd, timeout: one_shot.append(command) or ("ran", "", 0),
        )
        assert PowerShellExecutor.execute_command("out:x") == ("ran", 0)
        assert one_shot == ["out:x"]

    def test_repeated_start_failures_stop_retrying(self, fake_session_cls, monkeypatch):
        calls = []

        def broken_start(self, timeout=20.0):
            calls.append(1)
            raise SessionUnavailable("nope")

        monkeypatch.setattr(fake_session_cls, "start", broken_start)
        monkeypatch.setattr(service, "_one_shot", lambda *a: ("ran", "", 0))
        for _ in range(service._MAX_SESSION_START_FAILURES + 2):
            PowerShellExecutor.execute_command("out:x")
        assert len(calls) == service._MAX_SESSION_START_FAILURES

    def test_busy_session_sends_the_call_one_shot(self, fake_session_cls, monkeypatch):
        PowerShellExecutor.execute_command("pid")
        monkeypatch.setattr(service, "_one_shot", lambda *a: ("one-shot", "", 0))
        results = {}
        slow = threading.Thread(
            target=lambda: results.setdefault(
                "slow", PowerShellExecutor.execute_command("sleep:1;out:slow")
            )
        )
        slow.start()
        time.sleep(0.3)
        assert PowerShellExecutor.execute_command("out:fast") == ("one-shot", 0)
        slow.join()
        assert results["slow"] == ("slow", 0)

    def test_disabled_by_env_var(self, fake_session_cls, monkeypatch):
        monkeypatch.setenv(service.SESSION_ENV, "off")
        monkeypatch.setattr(service, "_one_shot", lambda *a: ("one-shot", "", 0))
        assert PowerShellExecutor.execute_command("out:x") == ("one-shot", 0)
        assert fake_session_cls.starts == 0

    def test_sessions_are_per_shell(self, fake_session_cls):
        PowerShellExecutor.execute_command("pid", shell="a")
        PowerShellExecutor.execute_command("pid", shell="b")
        assert fake_session_cls.starts == 2


class TestRealPowerShell:
    """The real host loop, through Windows PowerShell (works in ssh session 0)."""

    @pytest.fixture(autouse=True)
    def _session_on(self, monkeypatch):
        monkeypatch.delenv(service.SESSION_ENV, raising=False)

    def run(self, command, timeout=15):
        return PowerShellExecutor.execute_command(command, timeout, shell="powershell")

    def test_output_and_exit_status(self):
        assert self.run("Write-Output ok") == ("ok\r\n", 0)
        assert self.run("cmd /c exit 3")[1] == 1
        assert self.run("cmd /c exit 3; 'after'") == ("after\r\n", 0)

    def test_errors_are_readable_text(self):
        output, code = self.run(r"Get-Item C:\definitely\not\here")
        assert code == 1
        assert "Cannot find path" in output
        assert "CLIXML" not in output

    def test_last_statement_decides_status(self):
        assert self.run("Write-Error x; 'ok'") == ("ok\r\n", 0)
        assert self.run("throw 'boom'")[1] == 1

    def test_parse_error(self):
        output, code = self.run("if (")
        assert code == 1 and "__wmcp" not in output

    def test_unicode_and_here_string(self):
        assert self.run("'héllo 日本'") == ("héllo 日本\r\n", 0)
        script = '$t = @"\nline1\n"@\n$t'
        assert self.run(script) == ("line1\r\n", 0)

    def test_state_does_not_leak_between_commands(self):
        self.run(r"$env:WMCP_LEAK = '1'; Set-Location C:\; $plain = 1; function f { 1 }")
        output, _ = self.run(
            '"[$env:WMCP_LEAK][$plain][$(Get-Command f -ErrorAction SilentlyContinue)]";'
            " (Get-Location).Path"
        )
        lines = output.splitlines()
        assert lines[0] == "[][][]"
        assert os.path.normcase(lines[1]) == os.path.normcase(os.path.expanduser("~"))

    def test_break_and_continue_do_not_wedge_the_host(self):
        assert self.run("break")[1] == 0
        assert self.run("continue")[1] == 0
        assert self.run("'still here'") == ("still here\r\n", 0)

    def test_exit_code_and_recovery(self):
        assert self.run("exit 5") == ("", 5)
        assert self.run("'back'") == ("back\r\n", 0)

    def test_stdin_readers_see_eof(self):
        started = time.monotonic()
        self.run("cmd /c pause")
        assert time.monotonic() - started < 10

    def test_timeout_keeps_processes_from_earlier_commands(self):
        self.run("'warm'")
        pid_text, code = self.run(
            "(Start-Process ping -ArgumentList '-n','60','127.0.0.1' -WindowStyle Hidden"
            " -PassThru).Id"
        )
        assert code == 0
        earlier = int(pid_text.strip())
        try:
            assert self.run("Start-Sleep 30", timeout=2) == ("Command execution timed out", 1)
            assert psutil.pid_exists(earlier), "a timeout must not kill apps launched earlier"
            assert self.run("'after timeout'") == ("after timeout\r\n", 0)
        finally:
            subprocess.run(["taskkill", "/PID", str(earlier), "/F"], capture_output=True)

    def test_much_faster_than_one_shot_once_warm(self):
        self.run("'warm'")
        started = time.perf_counter()
        for _ in range(5):
            self.run("echo 1")
        assert (time.perf_counter() - started) / 5 < 0.5
