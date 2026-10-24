"""Find, attach to or launch a Chromium browser (Edge first, then Chrome) with a debugging port.

Chromium refuses ``--remote-debugging-port`` on the default user-data-dir, so the launched
browser uses a dedicated, persistent profile (logins and cookies stick between runs).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_PORT = 9222
LAUNCH_TIMEOUT = 20.0

_EDGE_NAMES = ("edge", "msedge", "microsoft-edge")
_CHROME_NAMES = ("chrome", "google-chrome")
# Launch flags: no first-run UI, and background/occluded tabs keep full speed so a minimized
# or covered browser still loads, renders and runs timers for the Browser tool.
BASE_ARGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--start-maximized",
)

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LaunchError(RuntimeError):
    pass


def browser_port() -> int:
    """``WINDOWS_MCP_BROWSER_PORT`` (default 9222)."""
    raw = os.getenv("WINDOWS_MCP_BROWSER_PORT", "").strip()
    try:
        port = int(raw) if raw else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


def profile_dir() -> Path:
    """``WINDOWS_MCP_BROWSER_PROFILE`` or ``<platformdirs user data>/browser-profile``."""
    raw = os.getenv("WINDOWS_MCP_BROWSER_PROFILE", "").strip()
    if raw:
        return Path(os.path.expandvars(raw)).expanduser()
    from platformdirs import user_data_dir

    return Path(user_data_dir("windows-mcp", appauthor=False)) / "browser-profile"


def extra_args() -> list[str]:
    """``WINDOWS_MCP_BROWSER_ARGS``: extra flags, whitespace separated (e.g. ``--headless``)."""
    return os.getenv("WINDOWS_MCP_BROWSER_ARGS", "").split()


def _app_path(exe: str) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as k:
                value, _ = winreg.QueryValueEx(k, None)
        except OSError:
            continue
        if value and Path(value.strip('"')).is_file():
            return value.strip('"')
    return None


def _known_paths(kind: str) -> list[Path]:
    roots = [
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("LOCALAPPDATA"),
    ]
    rel = (
        Path("Microsoft/Edge/Application/msedge.exe")
        if kind == "edge"
        else Path("Google/Chrome/Application/chrome.exe")
    )
    return [Path(r) / rel for r in roots if r]


def _find(kind: str) -> str | None:
    exe = "msedge.exe" if kind == "edge" else "chrome.exe"
    found = _app_path(exe)
    if found:
        return found
    for path in _known_paths(kind):
        if path.is_file():
            return str(path)
    names = ("microsoft-edge", "msedge") if kind == "edge" else ("google-chrome", "chromium")
    for name in names:
        hit = shutil.which(name)
        if hit:
            return hit
    return None


def find_executable() -> str:
    """``WINDOWS_MCP_BROWSER`` (``edge``, ``chrome`` or a path); default Edge, then Chrome."""
    raw = os.getenv("WINDOWS_MCP_BROWSER", "").strip().strip('"')
    if raw:
        low = raw.lower()
        if low in _EDGE_NAMES or low in _CHROME_NAMES:
            found = _find("edge" if low in _EDGE_NAMES else "chrome")
            if found:
                return found
            raise LaunchError(f"WINDOWS_MCP_BROWSER={raw}: browser not found")
        if Path(raw).is_file():
            return raw
        hit = shutil.which(raw)
        if hit:
            return hit
        raise LaunchError(f"WINDOWS_MCP_BROWSER={raw}: no such executable")
    for kind in ("edge", "chrome"):
        found = _find(kind)
        if found:
            return found
    raise LaunchError("No Edge or Chrome found; set WINDOWS_MCP_BROWSER to the executable")


def probe(port: int, timeout: float = 0.3) -> dict | None:
    """``/json/version`` of a browser listening on *port*, or None."""
    try:
        with _NO_PROXY.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8", "replace"))
    except OSError, ValueError:
        return None
    return info if isinstance(info, dict) and info.get("webSocketDebuggerUrl") else None


def _active_port(profile: Path) -> int | None:
    """Port written by a browser already running on *profile* (``DevToolsActivePort``)."""
    try:
        first = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0]
        return int(first.strip())
    except OSError, ValueError, IndexError:
        return None


def launch(exe: str, port: int, profile: Path, url: str | None = None) -> subprocess.Popen:
    """Start *exe* detached, with no inherited stdio (it must never touch the MCP stream)."""
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        *BASE_ARGS,
        *extra_args(),
        url or "about:blank",
    ]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def ensure_browser(url: str | None = None) -> tuple[dict, bool]:
    """Attach to the browser on the debugging port, launching it when nobody listens.

    Returns ``(version_info, launched)``; ``version_info["webSocketDebuggerUrl"]`` is the
    browser endpoint.
    """
    port = browser_port()
    info = probe(port)
    if info:
        return info, False
    profile = profile_dir()
    other = _active_port(profile)
    if other and other != port:
        info = probe(other)
        if info:
            return info, False
    exe = find_executable()
    proc = launch(exe, port, profile, url)
    deadline = time.monotonic() + LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        info = probe(port, timeout=0.5)
        if info:
            return info, True
        if proc.poll() is not None and proc.returncode not in (0, None):
            raise LaunchError(f"{exe} exited with code {proc.returncode}")
        time.sleep(0.03)
    raise LaunchError(
        f"{Path(exe).name} did not open a debugging port on {port} within "
        f"{LAUNCH_TIMEOUT:g}s. If a browser is already running on profile {profile} without "
        "a debugging port, close it and retry."
    )
