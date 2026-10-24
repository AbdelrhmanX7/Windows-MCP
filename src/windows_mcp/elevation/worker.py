"""The elevated worker: a small admin process that runs calls for a non-elevated server.

Started once per server session, either by the ``windows-mcp-elevated-worker``
scheduled task (``RunLevel Highest``, no prompt) or through ``runas`` (one UAC consent),
then reused for every ``elevated=true`` call until the server exits.

Protocol (``multiprocessing.connection`` over a named pipe the server created; both
sides prove the 32-byte key with the HMAC challenge before anything else is read):

* worker → server, once: ``("hello", {"pid", "is_admin", "integrity"})``
* server → worker: ``(id, op, args, kwargs)``; ops are the names in :data:`OPS`
* worker → server: ``(id, True, result)`` or ``(id, False, "ExcType: message")``

Each request runs on its own thread, so a long ``winget install`` does not hold up
other elevated calls; ``powershell`` requests share the worker's persistent PowerShell
session and fall back to a fresh process when it is busy, as on the server.

The worker exits when every connection it serves has closed (the server exited).

Run as ``python -m windows_mcp.elevation.worker --pipe <address> --key <hex>`` (runas),
or ``--rendezvous`` (scheduled task: connect to every request file the servers left in
:data:`REQUEST_DIR`).
"""

import argparse
import importlib
import json
import logging
import os
import sys
import threading
import time
from multiprocessing.connection import Client, Connection
from pathlib import Path

from windows_mcp.elevation.token import integrity_level, is_admin

__all__ = ["OPS", "REQUEST_DIR", "main", "serve_connection"]

logger = logging.getLogger(__name__)

STATE_DIR = Path("~/.windows-mcp").expanduser()
REQUEST_DIR = STATE_DIR / "elevation"
LOG_FILE = STATE_DIR / "elevated-worker.log"
# Request files older than this are stale (their server gave up waiting).
REQUEST_MAX_AGE_S = 120.0


def _powershell(command: str, timeout: int = 30, shell: str | None = None) -> tuple[str, int]:
    from windows_mcp.powershell.service import PowerShellExecutor

    return PowerShellExecutor.execute_command(command, timeout, shell)


def _target(spec: str):
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


def _hello() -> dict:
    # Must never raise: a failed hello would end the connection thread silently and
    # leave the server waiting for a worker that is already gone.
    try:
        integrity = integrity_level()
    except Exception:  # noqa: BLE001
        logger.debug("integrity_level failed", exc_info=True)
        integrity = "unknown"
    return {"pid": os.getpid(), "is_admin": is_admin(), "integrity": integrity}


# Operations a server may ask for. Module functions are resolved lazily, so the worker
# starts without importing the filesystem/process services until one is used.
OPS: dict[str, object] = {
    "ping": _hello,
    "powershell": _powershell,
    "filesystem.read_file": "windows_mcp.filesystem.service:read_file",
    "filesystem.write_file": "windows_mcp.filesystem.service:write_file",
    "filesystem.copy_path": "windows_mcp.filesystem.service:copy_path",
    "filesystem.move_path": "windows_mcp.filesystem.service:move_path",
    "filesystem.delete_path": "windows_mcp.filesystem.service:delete_path",
    "filesystem.list_directory": "windows_mcp.filesystem.service:list_directory",
    "filesystem.search_files": "windows_mcp.filesystem.service:search_files",
    "filesystem.get_file_info": "windows_mcp.filesystem.service:get_file_info",
    "process.kill_process": "windows_mcp.process.service:kill_process",
    "process.list_processes": "windows_mcp.process.service:list_processes",
}


def _resolve(op: str):
    target = OPS.get(op)
    if target is None:
        raise ValueError(f"unknown elevated operation {op!r}")
    return _target(target) if isinstance(target, str) else target


def serve_connection(conn: Connection) -> None:
    """Answer requests on ``conn`` until the server closes it.

    Args:
        conn: An authenticated connection to a server.
    """
    send_lock = threading.Lock()

    def send(message: tuple) -> None:
        with send_lock:
            conn.send(message)

    def handle(request_id: int, op: str, args: tuple, kwargs: dict) -> None:
        try:
            value = _resolve(op)(*args, **kwargs)
            reply = (request_id, True, value)
        except Exception as exc:
            logger.debug("Elevated op %s failed", op, exc_info=True)
            reply = (request_id, False, f"{type(exc).__name__}: {exc}")
        try:
            send(reply)
        except OSError, EOFError, ValueError:
            pass

    send(("hello", _hello()))
    while True:
        try:
            request_id, op, args, kwargs = conn.recv()
        except EOFError, OSError:
            break
        except Exception:
            logger.warning("Dropping a malformed elevated request", exc_info=True)
            continue
        threading.Thread(
            target=handle, args=(request_id, op, args, kwargs), daemon=True
        ).start()
    try:
        conn.close()
    except OSError:
        pass


def _connect(address: str, key: bytes) -> Connection:
    return Client(address, family="AF_PIPE", authkey=key)


def _take_requests() -> list[dict]:
    """Read and delete the pending request files the servers wrote."""
    requests = []
    now = time.time()
    try:
        files = sorted(REQUEST_DIR.glob("*.json"))
    except OSError:
        return []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            path.unlink()
        except OSError, ValueError:
            continue
        if now - float(data.get("created", 0)) <= REQUEST_MAX_AGE_S:
            requests.append(data)
    return requests


def main(argv: list[str] | None = None) -> int:
    """Entry point of the worker process."""
    parser = argparse.ArgumentParser(prog="windows_mcp.elevation.worker")
    parser.add_argument("--pipe", help="Named pipe address of the server.")
    parser.add_argument("--key", help="Hex authentication key for --pipe.")
    parser.add_argument(
        "--rendezvous",
        action="store_true",
        help=f"Serve every pending request file in {REQUEST_DIR}.",
    )
    options = parser.parse_args(argv)

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=LOG_FILE,
            level=logging.INFO,
            format="%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s",
        )
    except OSError:
        pass
    logger.info("Elevated worker starting (admin=%s, %s)", is_admin(), integrity_level())

    targets: list[tuple[str, bytes]] = []
    if options.pipe and options.key:
        targets.append((options.pipe, bytes.fromhex(options.key)))
    if options.rendezvous:
        targets.extend((r["pipe"], bytes.fromhex(r["key"])) for r in _take_requests())
    if not targets:
        logger.info("No server to serve; exiting")
        return 0

    threads = []
    for address, key in targets:
        try:
            conn = _connect(address, key)
        except Exception:
            logger.warning("Could not connect to %s", address, exc_info=True)
            continue
        logger.info("Serving %s", address)
        thread = threading.Thread(target=serve_connection, args=(conn,), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    logger.info("Elevated worker exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
