"""Raw readers for each installed-app source.

Each reader returns plain dicts so the merge step (``service.merge_sources``)
can be tested without Windows. PowerShell is used only for what has no cheap
Python equivalent (Get-StartApps, the AppsFolder shell namespace, Get-AppxPackage),
and all three run in one ``powershell.exe`` invocation. The uninstall keys and
URL protocol handlers are read with ``winreg`` directly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

# One PowerShell call: Start-menu apps, AppsFolder display names, AppX packages.
# Each section is wrapped in @() so a single result still serializes as an array.
_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$start = @(Get-StartApps | ForEach-Object { [pscustomobject]@{ Name = $_.Name; AppID = $_.AppID } })
$folder = @()
try {
  $folder = @((New-Object -ComObject Shell.Application).NameSpace('shell:::{4234d49b-0245-4df3-b780-3893943456e1}').Items() |
    ForEach-Object { [pscustomobject]@{ Name = $_.Name; AppID = $_.Path } })
} catch {}
$appx = @(Get-AppxPackage | Where-Object { -not $_.IsFramework } | ForEach-Object {
  [pscustomobject]@{
    Name = $_.Name; PackageFamilyName = $_.PackageFamilyName; InstallLocation = $_.InstallLocation;
    Version = [string]$_.Version; Publisher = $_.Publisher
  } })
[pscustomobject]@{ startapps = $start; appsfolder = $folder; appx = $appx } | ConvertTo-Json -Depth 4 -Compress
"""

PS_TIMEOUT_SEC = 45


def read_shell_sources() -> dict[str, list[dict]]:
    """Run the combined PowerShell query. Returns ``{startapps, appsfolder, appx}``."""
    from windows_mcp.powershell.service import PowerShellExecutor

    output, status = PowerShellExecutor.execute_command(_PS_SCRIPT, timeout=PS_TIMEOUT_SEC)
    empty = {"startapps": [], "appsfolder": [], "appx": []}
    if status != 0 or not output or not output.strip():
        logger.warning("App inventory PowerShell query failed (status=%s)", status)
        return empty
    text = output.strip()
    start = text.find("{")
    try:
        data = json.loads(text[start:] if start >= 0 else text)
    except json.JSONDecodeError:
        logger.warning("App inventory PowerShell output was not JSON")
        return empty
    return {key: _as_list(data.get(key)) for key in empty}


def _as_list(value) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return [v for v in value if isinstance(v, dict)]


# ---------------------------------------------------------------------------
# Registry: Win32 uninstall entries
# ---------------------------------------------------------------------------

_UNINSTALL_PATHS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)
_UNINSTALL_VALUES = (
    "DisplayName",
    "DisplayVersion",
    "Publisher",
    "InstallLocation",
    "DisplayIcon",
    "SystemComponent",
    "ParentKeyName",
    "ReleaseType",
)


def read_uninstall_entries() -> list[dict]:
    """Read HKLM/HKCU (and WOW6432Node) uninstall keys with winreg."""
    import winreg

    entries: list[dict] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for path in _UNINSTALL_PATHS:
            try:
                root = winreg.OpenKey(hive, path)
            except OSError:
                continue
            with root:
                index = 0
                while True:
                    try:
                        sub = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(root, sub) as key:
                            entry = {"KeyName": sub}
                            for value_name in _UNINSTALL_VALUES:
                                try:
                                    entry[value_name] = winreg.QueryValueEx(key, value_name)[0]
                                except OSError:
                                    pass
                    except OSError:
                        continue
                    if entry.get("DisplayName"):
                        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Registry: URL protocol handlers
# ---------------------------------------------------------------------------


def read_url_protocols() -> list[dict]:
    """Enumerate HKCR keys carrying a ``URL Protocol`` value.

    Returns ``[{"scheme": "spotify", "command": "...", "exe": "C:\\...\\Spotify.exe"}]``.
    ``exe`` is ``None`` when the handler has no ``shell\\open\\command`` (packaged
    apps, which are mapped through their AppX manifest instead).
    """
    import winreg

    protocols: list[dict] = []
    root = winreg.HKEY_CLASSES_ROOT
    index = 0
    while True:
        try:
            name = winreg.EnumKey(root, index)
        except OSError:
            break
        index += 1
        if not name or name[0] in ".{*" or not _SCHEME_RE.match(name):
            continue
        try:
            with winreg.OpenKey(root, name) as key:
                winreg.QueryValueEx(key, "URL Protocol")
        except OSError:
            continue
        command = None
        try:
            with winreg.OpenKey(root, name + r"\shell\open\command") as key:
                command = winreg.QueryValueEx(key, "")[0]
        except OSError:
            pass
        protocols.append(
            {"scheme": name.lower(), "command": command, "exe": exe_from_command(command)}
        )
    return protocols


_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*$")


def exe_from_command(command: str | None) -> str | None:
    """Extract the executable path from a shell command line or DisplayIcon value."""
    if not command:
        return None
    command = os.path.expandvars(command.strip())
    if command.startswith('"'):
        end = command.find('"', 1)
        path = command[1:end] if end > 0 else command[1:]
    else:
        match = re.match(r"(.+?\.exe)\b", command, re.IGNORECASE)
        path = match.group(1) if match else command.split(",")[0].split(" ")[0]
    path = path.strip().strip('"')
    path = re.sub(r",\s*-?\d+$", "", path)  # DisplayIcon "app.exe,0"
    if not path.lower().endswith(".exe"):
        return None
    return path


# ---------------------------------------------------------------------------
# AppX manifests and known-folder AppIDs
# ---------------------------------------------------------------------------


def read_appx_manifest(install_location: str | None) -> dict:
    """Parse an AppxManifest.xml for application ids, executables and protocols.

    Returns ``{"applications": [{"id", "executable"}], "protocols": [str]}``.
    """
    result: dict = {"applications": [], "protocols": []}
    if not install_location:
        return result
    path = Path(install_location) / "AppxManifest.xml"
    try:
        tree = ET.parse(path)
    except OSError, ET.ParseError:
        return result
    for element in tree.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "Application":
            result["applications"].append(
                {"id": element.get("Id"), "executable": element.get("Executable")}
            )
        elif tag == "Protocol" and element.get("Name"):
            scheme = element.get("Name").lower()
            if scheme not in result["protocols"]:
                result["protocols"].append(scheme)
    return result


_KNOWN_FOLDER_RE = re.compile(r"^\{([0-9A-Fa-f\-]{36})\}\\(.+)$")
_known_folder_cache: dict[str, str | None] = {}


def resolve_known_folder_path(app_id: str) -> str | None:
    """Turn ``{KnownFolderGUID}\\rel\\app.exe`` (Start-menu AppIDs) into a real path."""
    match = _KNOWN_FOLDER_RE.match(app_id or "")
    if not match:
        if re.match(r"^[A-Za-z]:\\", app_id or ""):
            return app_id
        return None
    guid, rest = match.group(1).upper(), match.group(2)
    if guid not in _known_folder_cache:
        _known_folder_cache[guid] = _known_folder(guid)
    base = _known_folder_cache[guid]
    return str(Path(base) / rest) if base else None


def _known_folder(guid: str) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        raw = bytes.fromhex(guid.replace("-", ""))
        g = GUID(
            int.from_bytes(raw[0:4], "big"),
            int.from_bytes(raw[4:6], "big"),
            int.from_bytes(raw[6:8], "big"),
            (ctypes.c_ubyte * 8)(*raw[8:16]),
        )
        out = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
        if hr != 0:
            return None
        value = out.value
        ctypes.windll.ole32.CoTaskMemFree(out)
        return value
    except Exception:
        return None
