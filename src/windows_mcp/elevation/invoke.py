"""Thin helper for tools that run a service operation elevated and return a string.

Keeps the ``elevated=true`` branches in the FileSystem and Process tools to one line and
turns "no worker available" / "worker failed" into a message the planner can act on.
"""

from windows_mcp.elevation.client import ELEVATION_ENV, ElevationUnavailable, call_elevated

__all__ = ["run_elevated_text"]


def run_elevated_text(op: str, *args, **kwargs) -> str:
    """Run ``op`` elevated and return its string result, or an error string.

    Args:
        op: An operation name from ``worker.OPS`` whose function returns a string.
        *args: Positional arguments for it.
        **kwargs: Keyword arguments for it.

    Returns:
        The operation's result, or a human-readable error (never raises).
    """
    try:
        return call_elevated(op, *args, **kwargs)
    except ElevationUnavailable as exc:
        return (
            f"Error: elevated execution unavailable: {exc}\n"
            "Install the no-prompt worker with `windows-mcp install-elevated`, "
            f"or set {ELEVATION_ENV}=runas to allow a UAC prompt."
        )
    except RuntimeError as exc:
        return f"Error: elevated execution failed: {exc}"
