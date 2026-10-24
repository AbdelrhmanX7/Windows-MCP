"""Elevation: the worker protocol, the server-side client, setup tasks and status.

The transport is exercised with an in-process ``multiprocessing.connection.Pipe`` so the
tests are platform-independent; the real launch paths (``runas``/Task Scheduler) are
covered by the e2e run on the VM.
"""

import threading
from multiprocessing.connection import Pipe

import pytest

from windows_mcp.elevation import client as elev_client
from windows_mcp.elevation import setup, worker
from windows_mcp.elevation.client import ElevatedWorker, ElevationUnavailable, call_elevated


def _recv(conn, timeout: float = 10.0):
    """recv() that fails the test instead of hanging when nothing arrives."""
    assert conn.poll(timeout), "no message from the worker"
    return conn.recv()


def _serve(conn) -> threading.Thread:
    thread = threading.Thread(target=worker.serve_connection, args=(conn,), daemon=True)
    thread.start()
    return thread


def test_ops_cover_every_tool_entry_point() -> None:
    for name in (
        "ping",
        "powershell",
        "filesystem.write_file",
        "filesystem.delete_path",
        "process.kill_process",
    ):
        assert name in worker.OPS


def test_worker_hello_and_ping_round_trip() -> None:
    server_end, worker_end = Pipe()
    _serve(worker_end)
    tag, info = _recv(server_end)
    assert tag == "hello"
    assert set(info) == {"pid", "is_admin", "integrity"}

    server_end.send((7, "ping", (), {}))
    request_id, ok, payload = _recv(server_end)
    assert (request_id, ok) == (7, True)
    assert payload["pid"] == info["pid"]
    server_end.close()


def test_worker_reports_failure_without_dropping_the_connection() -> None:
    server_end, worker_end = Pipe()
    _serve(worker_end)
    _recv(server_end)  # hello

    server_end.send((1, "does-not-exist", (), {}))
    request_id, ok, payload = _recv(server_end)
    assert request_id == 1
    assert ok is False
    assert "unknown elevated operation" in payload

    # A later valid call still works: one failed request must not kill the worker.
    server_end.send((2, "ping", (), {}))
    assert _recv(server_end)[0] == 2
    server_end.close()


def _connected_worker(monkeypatch) -> tuple[ElevatedWorker, object]:
    """An ElevatedWorker already wired to an in-process worker over a Pipe."""
    server_end, worker_end = Pipe()
    _serve(worker_end)
    w = ElevatedWorker()
    # Pretend the worker already connected and sent a hello.
    w._conn = server_end
    assert _recv(server_end)[0] == "hello"
    w._info = {"pid": 1, "is_admin": True, "integrity": "high"}
    w._launched_by = "task"
    w._reader = threading.Thread(target=w._read_loop, daemon=True)
    w._reader.start()
    return w, server_end


def test_client_call_returns_worker_result(monkeypatch) -> None:
    w, _ = _connected_worker(monkeypatch)
    result = w.call("ping", (), {}, timeout=5)
    assert set(result) == {"pid", "is_admin", "integrity"}
    assert result["is_admin"] == worker.is_admin()
    w.close()


def test_client_rejects_unknown_op(monkeypatch) -> None:
    w, _ = _connected_worker(monkeypatch)
    with pytest.raises(ValueError, match="unknown elevated operation"):
        w.call("nope", (), {}, timeout=5)
    w.close()


def test_client_surfaces_worker_exception_as_runtimeerror(monkeypatch) -> None:
    w, _ = _connected_worker(monkeypatch)
    # A valid op given a bad keyword makes the worker function raise TypeError,
    # which must come back to the caller as a RuntimeError carrying the message.
    with pytest.raises(RuntimeError, match="TypeError"):
        w.call("filesystem.get_file_info", (), {"no_such_kwarg": 1}, timeout=5)
    w.close()


def test_client_drop_wakes_every_pending_waiter() -> None:
    # A worker that dies mid-request must not leave callers blocked until timeout:
    # _drop() wakes every pending waiter with a failure.
    w = ElevatedWorker()
    event = threading.Event()
    slot = {"event": event}
    w._pending[0] = slot

    w._drop()

    assert event.wait(2)
    assert slot["ok"] is False
    assert "exited" in slot["payload"]


def test_call_elevated_runs_in_process_when_already_admin(monkeypatch) -> None:
    monkeypatch.setattr(elev_client, "is_admin", lambda: True)
    result = call_elevated("ping")
    assert result["is_admin"] == worker.is_admin()


def test_call_elevated_unavailable_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(elev_client, "is_admin", lambda: False)
    monkeypatch.setenv(elev_client.ELEVATION_ENV, "off")
    elev_client.shutdown_worker()
    with pytest.raises(ElevationUnavailable):
        call_elevated("ping")
    elev_client.shutdown_worker()


@pytest.mark.parametrize("value,expected", [("auto", "auto"), ("TASK", "task"), ("bogus", "auto")])
def test_launch_mode_parsing(monkeypatch, value, expected) -> None:
    monkeypatch.setenv(elev_client.ELEVATION_ENV, value)
    assert elev_client.launch_mode() == expected


def test_register_script_asks_for_highest_interactive() -> None:
    script = setup._register_script("t", "cmd.exe", "/c echo hi")
    assert "-RunLevel Highest" in script
    assert "-LogonType Interactive" in script
    assert "Register-ScheduledTask -TaskName 't'" in script


def test_setup_install_requires_elevation_and_registers_both(monkeypatch) -> None:
    calls: list[str] = []

    def fake_ps(script: str, timeout: float = 60):
        import types

        calls.append(script)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup, "_run_powershell", fake_ps)
    registered = setup.install(host="127.0.0.1", port=9001)
    assert registered == [setup.SERVER_TASK_NAME, setup.WORKER_TASK_NAME]
    assert any("--port 9001" in c for c in calls)
    assert any("elevation.worker" in c and "--rendezvous" in c for c in calls)


def test_worker_status_before_start(monkeypatch) -> None:
    monkeypatch.setattr(ElevatedWorker, "task_installed", lambda self: False)
    w = ElevatedWorker()
    status = w.status()
    assert status["running"] is False
    assert status["task_installed"] is False
