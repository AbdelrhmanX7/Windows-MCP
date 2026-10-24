"""Admin mode: report elevation, run the server elevated, and an elevated worker.

See ``docs/INTEGRATION.md`` (admin mode) and the README. The three public pieces:

* :mod:`windows_mcp.elevation.token` -- is this process elevated?
* :mod:`windows_mcp.elevation.client` -- :func:`call_elevated` for ``elevated=true`` tool
  arguments, backed by a persistent elevated worker when the server is not itself admin.
* :mod:`windows_mcp.elevation.setup` -- install/uninstall the no-prompt scheduled tasks.
"""

from windows_mcp.elevation.token import integrity_level, is_admin, uac_enabled

__all__ = ["elevation_status", "integrity_level", "is_admin", "uac_enabled"]


def elevation_status() -> dict:
    """Return a JSON-safe snapshot of the server's elevation and worker state.

    Used by ``ControlStatus`` and the startup banner.
    """
    from windows_mcp.elevation.client import get_worker, launch_mode

    admin = is_admin()
    status = {
        "is_admin": admin,
        "integrity": integrity_level(),
        "uac_enabled": uac_enabled(),
        "elevation_mode": launch_mode(),
    }
    # Only describe the worker once it has been used, so reading status never starts it.
    worker = get_worker()
    status["elevated_worker"] = worker.status() if worker.running else {
        "running": False,
        "mode": launch_mode(),
        "task_installed": worker.task_installed(),
    }
    return status
