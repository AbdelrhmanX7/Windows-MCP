"""Server side of the elevated worker: start it once, then send it calls.

See ``elevation/worker.py`` for the protocol. :func:`call_elevated` is what tools use;
when the server itself is already elevated it runs the operation in-process instead.

Launch strategy (``WINDOWS_MCP_ELEVATION``):

* ``auto`` (default): the ``windows-mcp-elevated-worker`` scheduled task when it is
  installed (no prompt), otherwise ``runas`` (one UAC consent per server session).
* ``task`` / ``runas``: only that one.
* ``off``: ``elevated=true`` fails unless the server is already elevated.
"""

import json
import logging
import os
import secrets
import sys
import threading
import time
from multiprocessing.connection import Connection, Listener

from windows_mcp.elevation import launch
from windows_mcp.elevation.token import is_admin
from windows_mcp.elevation.worker import OPS, REQUEST_DIR

__all__ = [
    "ELEVATION_ENV",
    "WORKER_TASK_NAME",
    "ElevatedWorker",
    "call_elevated",
    "get_worker",
    "launch_mode",
    "shutdown_worker",
]

logger = logging.getLogger(__name__)

ELEVATION_ENV = "WINDOWS_MCP_ELEVATION"
WORKER_TASK_NAME = "windows-mcp-elevated-worker"
_MODES = ("auto", "task", "runas", "off")
# runas waits for a human to answer the consent dialog; a task starts in about a second.
_RUNAS_CONNECT_TIMEOUT_S = 120.0
_TASK_CONNECT_TIMEOUT_S = 30.0
_HELLO_TIMEOUT_S = 15.0
# Extra seconds past an operation's own timeout before the server stops waiting.
_REPLY_GRACE_S = 30.0
_DEFAULT_REPLY_TIMEOUT_S = 300.0
# Seconds a scheduled-task presence check stays cached.
_TASK_CHECK_TTL_S = 60.0


class ElevationUnavailable(launch.ElevationError):
    """No elevated worker could be started and the server is not elevated."""


def launch_mode() -> str:
    """Return the configured launch strategy (``WINDOWS_MCP_ELEVATION``)."""
    value = os.environ.get(ELEVATION_ENV, "auto").strip().lower()
    return value if value in _MODES else "auto"


def _pipe_address() -> str:
    """A unique named-pipe address for this server's worker rendezvous."""
    return rf"\\.\pipe\windows-mcp-elev-{os.getpid()}-{secrets.token_hex(8)}"


class ElevatedWorker:
    """One elevated worker process and the connection to it. Thread-safe."""

    def __init__(self) -> None:
        self._start_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._conn: Connection | None = None
        self._pending: dict[int, dict] = {}
        self._next_id = 0
        self._info: dict | None = None
        self._launched_by: str | None = None
        self._process: launch.ElevatedProcess | None = None
        self._last_error: str | None = None
        self._reader: threading.Thread | None = None
        self._task_checked_at = 0.0
        self._task_installed = False

    @property
    def running(self) -> bool:
        """True while connected to a live worker."""
        return self._conn is not None

    def task_installed(self) -> bool:
        """Whether the no-prompt worker task is registered (cached briefly)."""
        now = time.monotonic()
        if not self._task_checked_at or now - self._task_checked_at > _TASK_CHECK_TTL_S:
            self._task_installed = launch.task_exists(WORKER_TASK_NAME)
            self._task_checked_at = now
        return self._task_installed

    def status(self) -> dict:
        """Return a JSON-safe description of the worker for ControlStatus/server info."""
        info = self._info or {}
        return {
            "running": self.running,
            "pid": info.get("pid") if self.running else None,
            "launched_by": self._launched_by if self.running else None,
            "mode": launch_mode(),
            "task_installed": self.task_installed(),
            "last_error": self._last_error,
        }

    def ensure(self) -> None:
        """Start the worker if it is not running.

        Raises:
            ElevationUnavailable: When no elevated worker could be started.
        """
        if self._conn is not None:
            return
        with self._start_lock:
            if self._conn is not None:
                return
            mode = launch_mode()
            if mode == "off":
                raise ElevationUnavailable(f"elevation disabled ({ELEVATION_ENV}=off)")
            key = secrets.token_bytes(32)
            address = _pipe_address()
            listener = Listener(address, family="AF_PIPE", authkey=key)
            try:
                launched_by = self._launch(mode, address, key)
                conn = self._accept(listener, launched_by)
            except launch.ElevationError as exc:
                self._last_error = str(exc)
                listener.close()
                raise ElevationUnavailable(str(exc)) from exc
            finally:
                try:
                    listener.close()
                except OSError:
                    pass
            self._conn = conn
            self._launched_by = launched_by
            self._last_error = None
            self._reader = threading.Thread(
                target=self._read_loop, name="elevated-worker-reader", daemon=True
            )
            self._reader.start()
            logger.info("Elevated worker ready via %s: %s", launched_by, self._info)

    def _launch(self, mode: str, address: str, key: bytes) -> str:
        """Start the worker; return the method that was used ("task" or "runas")."""
        use_task = mode in ("auto", "task") and self.task_installed()
        if use_task:
            self._write_request(address, key)
            try:
                launch.run_task(WORKER_TASK_NAME)
                return "task"
            except launch.ElevationError:
                if mode == "task":
                    raise
                logger.debug("Worker task failed to run; falling back to runas", exc_info=True)
        if mode == "task":
            raise launch.ElevationError(
                f"the {WORKER_TASK_NAME!r} task is not installed "
                "(run `windows-mcp install-elevated`)"
            )
        self._process = launch.shell_execute_runas(
            sys.executable,
            ["-m", "windows_mcp.elevation.worker", "--pipe", address, "--key", key.hex()],
        )
        return "runas"

    def _write_request(self, address: str, key: bytes) -> None:
        """Leave a rendezvous file the scheduled-task worker picks up and connects back on."""
        REQUEST_DIR.mkdir(parents=True, exist_ok=True)
        path = REQUEST_DIR / f"{os.getpid()}-{secrets.token_hex(6)}.json"
        payload = {"pipe": address, "key": key.hex(), "created": time.time()}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)

    def _accept(self, listener: Listener, launched_by: str) -> Connection:
        """Wait for the worker to connect back and send its hello."""
        timeout = _TASK_CONNECT_TIMEOUT_S if launched_by == "task" else _RUNAS_CONNECT_TIMEOUT_S
        result: dict = {}
        done = threading.Event()

        def wait() -> None:
            try:
                result["conn"] = listener.accept()
            except Exception as exc:  # noqa: BLE001 - surfaced below
                result["error"] = exc
            finally:
                done.set()

        threading.Thread(target=wait, name="elevated-worker-accept", daemon=True).start()
        if not done.wait(timeout):
            raise launch.ElevationError(
                f"the elevated worker did not connect within {int(timeout)} s"
            )
        if "error" in result:
            raise launch.ElevationError(f"worker connection failed: {result['error']}")
        conn = result["conn"]
        if not conn.poll(_HELLO_TIMEOUT_S):
            conn.close()
            raise launch.ElevationError("the elevated worker did not send its hello")
        tag, info = conn.recv()
        if tag != "hello":
            conn.close()
            raise launch.ElevationError(f"unexpected first message from worker: {tag!r}")
        if not info.get("is_admin"):
            conn.close()
            raise launch.ElevationError("the worker started without administrator rights")
        self._info = info
        return conn

    def _read_loop(self) -> None:
        """Deliver each reply to the waiting caller until the connection drops."""
        conn = self._conn
        while conn is not None:
            try:
                request_id, ok, payload = conn.recv()
            except EOFError, OSError:
                break
            except Exception:
                logger.debug("Dropping a malformed worker reply", exc_info=True)
                continue
            with self._pending_lock:
                slot = self._pending.get(request_id)
            if slot is not None:
                slot["ok"] = ok
                slot["payload"] = payload
                slot["event"].set()
        self._drop()

    def _drop(self) -> None:
        """Tear down a dead connection and wake every waiter."""
        with self._start_lock:
            conn, self._conn = self._conn, None
            self._info = None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for slot in waiters:
            slot.setdefault("payload", "RuntimeError: elevated worker exited")
            slot["ok"] = False
            slot["event"].set()

    def call(self, op: str, args: tuple, kwargs: dict, timeout: float):
        """Run one operation on the worker and return its result.

        Raises:
            ElevationUnavailable: When the worker could not be started.
            RuntimeError: When the worker died before replying.
            Exception: Re-raised from the worker as a ``RuntimeError`` carrying its message.
        """
        if op not in OPS:
            raise ValueError(f"unknown elevated operation {op!r}")
        self.ensure()
        event = threading.Event()
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = {"event": event}
        try:
            with self._send_lock:
                if self._conn is None:
                    raise RuntimeError("elevated worker exited")
                self._conn.send((request_id, op, args, kwargs))
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self._drop()
            raise RuntimeError(f"elevated worker send failed: {exc}") from exc
        if not event.wait(timeout + _REPLY_GRACE_S):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise RuntimeError(f"elevated worker did not reply within {int(timeout)} s")
        with self._pending_lock:
            slot = self._pending.pop(request_id, None)
        slot = slot or {}
        if slot.get("ok"):
            return slot.get("payload")
        raise RuntimeError(str(slot.get("payload", "elevated worker error")))

    def close(self) -> None:
        """Stop the worker by closing the connection (it exits when its servers do)."""
        with self._start_lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        process, self._process = self._process, None
        if process is not None:
            try:
                process.close()
            except OSError:
                pass


_worker: ElevatedWorker | None = None
_worker_lock = threading.Lock()


def get_worker() -> ElevatedWorker:
    """Return the process-wide elevated worker handle (created on first use)."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = ElevatedWorker()
        return _worker


def shutdown_worker() -> None:
    """Close the elevated worker, if one was started."""
    global _worker
    with _worker_lock:
        worker, _worker = _worker, None
    if worker is not None:
        worker.close()


def call_elevated(op: str, *args, timeout: float = _DEFAULT_REPLY_TIMEOUT_S, **kwargs):
    """Run ``op`` with administrator rights.

    When the server is already elevated the operation runs in-process; otherwise it is
    sent to the persistent elevated worker (started on the first such call).

    Args:
        op: An operation name from ``worker.OPS``.
        *args: Positional arguments for it.
        timeout: Seconds to let it run before giving up.
        **kwargs: Keyword arguments for it.

    Returns:
        Whatever the operation returns.

    Raises:
        ElevationUnavailable: When no elevated worker could be started.
        RuntimeError: When the worker failed; its message is preserved.
    """
    if is_admin():
        from windows_mcp.elevation.worker import _resolve

        return _resolve(op)(*args, **kwargs)
    return get_worker().call(op, args, kwargs, timeout)
