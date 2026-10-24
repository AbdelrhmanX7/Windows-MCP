"""Auto-generate a skill for an installed app from what its UI exposes.

Pipeline (each step is a separate function so tests can drive the pure parts):

1. :func:`ensure_window` finds the app's top-level window, launching it if needed.
2. :func:`read_win32_menu` reads a classic menu bar via ``GetMenuString``. Menu text
   carries ``&`` access keys and ``\\t`` accelerators (``&Save\\tCtrl+S``), so this
   yields real shortcuts without opening any menu.
3. :func:`walk_uia` walks the UIA tree shallowly (time and element bounded) and keeps
   menu items, buttons and tabs with their ``AcceleratorKey`` / ``AccessKey``.
4. :func:`build_skill` turns that plus the inventory record into a ``generated`` skill.
5. :func:`write_generated` saves it to the user folder without clobbering anything
   that is not itself generated.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from windows_mcp.apps.models import AppRecord, slugify
from windows_mcp.skills.models import Skill
from windows_mcp.skills.store import SkillStore

logger = logging.getLogger(__name__)

LAUNCH_TIMEOUT_SEC = 10.0
WALK_TIME_LIMIT_SEC = 5.0
WALK_MAX_ELEMENTS = 600
WALK_MAX_DEPTH = 12
SETTLE_SEC = 3.0  # ribbons and lazy UIs finish building after the window appears
_KEEP_TYPES = {
    "MenuItemControl",
    "ButtonControl",
    "SplitButtonControl",
    "TabItemControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "ComboBoxControl",
    "HyperlinkControl",
}
# Containers that never hold commands worth recording (documents, long lists).
_SKIP_SUBTREES = {
    "DocumentControl",
    "EditControl",
    "ListControl",
    "TreeControl",
    "DataGridControl",
    "TableControl",
}
_TITLE_BAR_NAMES = {"minimize", "maximize", "restore", "close", "system", "system menu"}


class GenerationError(RuntimeError):
    """Raised when a skill cannot be generated (no window, or a protected file)."""


@dataclass
class UIElementInfo:
    name: str
    control_type: str
    accelerator: str = ""
    access_key: str = ""
    path: str = ""  # "File > Save" for menu entries


@dataclass
class AppUIInfo:
    """What generation learned from the app's window."""

    window_title: str = ""
    menus: dict[str, list[UIElementInfo]] = field(default_factory=dict)
    menu_access_keys: dict[str, str] = field(default_factory=dict)
    elements: list[UIElementInfo] = field(default_factory=list)
    visited: int = 0
    truncated: bool = False
    elapsed_sec: float = 0.0


# ---------------------------------------------------------------------------
# Window discovery and launch (Windows only)
# ---------------------------------------------------------------------------


def _process_name(pid: int) -> str:
    try:
        import psutil

        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def find_window(app: AppRecord) -> int | None:
    """Return the handle of a visible top-level window owned by *app*, if any."""
    import win32gui
    import win32process

    targets = set(app.process_names)
    names = [n for n in app.all_names() if len(n) >= 3]
    found: list[int] = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = _process_name(pid)
        if process in targets and process != "explorer.exe":
            found.append(hwnd)
        elif process in ("applicationframehost.exe", "explorer.exe") and any(
            title.lower() == n or title.lower().endswith(" - " + n) for n in names
        ):
            found.append(hwnd)
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        logger.debug("EnumWindows failed", exc_info=True)
    return found[0] if found else None


def launch(app: AppRecord) -> None:
    """Start *app* via its AUMID (preferred) or executable."""
    if app.aumid:
        subprocess.Popen(
            ["explorer.exe", f"shell:AppsFolder\\{app.aumid}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    elif app.exe and Path(app.exe).is_file():
        subprocess.Popen(
            [app.exe],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    else:
        raise GenerationError(f"{app.name} has no AUMID or executable to launch")


def ensure_window(app: AppRecord, timeout: float = LAUNCH_TIMEOUT_SEC) -> tuple[int, bool]:
    """Return ``(hwnd, launched)``; launch the app and wait for its window if needed."""
    hwnd = find_window(app)
    if hwnd:
        return hwnd, False
    launch(app)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.4)
        hwnd = find_window(app)
        if hwnd:
            time.sleep(SETTLE_SEC)
            return hwnd, True
    raise GenerationError(f"No window for {app.name} appeared within {timeout:.0f}s")


def close_window(hwnd: int) -> None:
    try:
        import win32con
        import win32gui

        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        logger.debug("Closing window %s failed", hwnd, exc_info=True)


# ---------------------------------------------------------------------------
# Reading the UI
# ---------------------------------------------------------------------------


def split_menu_text(text: str) -> tuple[str, str, str]:
    """``"&Save As...\\tCtrl+Shift+S"`` -> ``("Save As", "S", "Ctrl+Shift+S")``."""
    label, _, accelerator = text.partition("\t")
    label = label.replace("&&", "\0")
    match = re.search(r"&(.)", label)
    access = match.group(1).upper() if match and match.group(1) != "\0" else ""
    label = label.replace("&", "").replace("\0", "&")
    label = label.strip().rstrip(".").rstrip("\u2026").strip()
    return label, access, accelerator.strip()


def read_win32_menu(
    hwnd: int, max_items: int = 300
) -> tuple[dict[str, list[UIElementInfo]], dict[str, str]]:
    """Read a classic Win32 menu bar without opening it.

    Returns ``(menus, menu_access_keys)``: items per top-level menu, and the
    access key of each top-level menu (``{"File": "F"}``).
    """
    import ctypes

    user32 = ctypes.windll.user32
    MF_BYPOSITION = 0x400
    menus: dict[str, list[UIElementInfo]] = {}
    access_keys: dict[str, str] = {}
    hmenu = user32.GetMenu(hwnd)
    if not hmenu:
        return menus, access_keys

    def text_at(menu, pos) -> str:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetMenuStringW(menu, pos, buf, 256, MF_BYPOSITION)
        return buf.value

    count = 0
    for top in range(max(0, user32.GetMenuItemCount(hmenu))):
        top_label, top_access, _ = split_menu_text(text_at(hmenu, top))
        if not top_label:
            continue
        access_keys[top_label] = top_access
        items: list[UIElementInfo] = []
        sub = user32.GetSubMenu(hmenu, top)
        for pos in range(max(0, user32.GetMenuItemCount(sub)) if sub else 0):
            raw = text_at(sub, pos)
            if not raw:
                continue  # separator
            label, access, accel = split_menu_text(raw)
            items.append(
                UIElementInfo(label, "MenuItemControl", accel, access, f"{top_label} > {label}")
            )
            count += 1
            if count >= max_items:
                break
        menus[top_label] = items
    return menus, access_keys


_NAME_SHORTCUT_RE = re.compile(r"\(((?:Ctrl|Alt|Shift|Win)\+[^)]+|F\d{1,2})\)")


def _useful_name(name: str) -> bool:
    return bool(name) and name.lower() not in _TITLE_BAR_NAMES and any(ch.isalnum() for ch in name)


def _children(control) -> list:
    """GetChildren, falling back to the sibling walker (some ribbons fail FindAll)."""
    try:
        return control.GetChildren()
    except Exception:
        pass
    children = []
    try:
        child = control.GetFirstChildControl()
        while child is not None and len(children) < 200:
            children.append(child)
            child = child.GetNextSiblingControl()
    except Exception:
        logger.debug("Child walk failed", exc_info=True)
    return children


def is_keytip(access_key: str) -> bool:
    """Ribbon KeyTips are comma-separated key sequences: ``"Alt, H, B"``."""
    return "," in access_key


def keytip_to_shortcuts(access_key: str) -> list[str]:
    """``"Alt, H, B"`` -> ``["alt", "h", "b"]`` (one Shortcut call per key); else ``[]``."""
    parts = [part.strip() for part in access_key.split(",") if part.strip()]
    if not parts or parts[0].lower() != "alt" or any(len(p) != 1 for p in parts[1:]):
        return []  # multi-character or disambiguated KeyTips: leave them to the table
    return [part.lower() for part in parts]


def walk_uia(
    hwnd: int,
    *,
    time_limit: float = WALK_TIME_LIMIT_SEC,
    max_elements: int = WALK_MAX_ELEMENTS,
    max_depth: int = WALK_MAX_DEPTH,
) -> AppUIInfo:
    """Breadth-first UIA walk from *hwnd*, bounded by time, element count and depth.

    Must run on the UIA worker thread (see ``run_uia_affine``).
    """
    from windows_mcp import uia

    info = AppUIInfo()
    started = time.monotonic()
    root = uia.ControlFromHandle(hwnd)
    if root is None:
        raise GenerationError("Window has no UI Automation element")
    try:
        info.window_title = root.Name or ""
    except Exception:
        pass
    queue: list[tuple[object, int]] = [(root, 0)]
    seen: dict[str, UIElementInfo] = {}
    while queue:
        if info.visited >= max_elements or time.monotonic() - started > time_limit:
            info.truncated = True
            break
        control, depth = queue.pop(0)
        info.visited += 1
        try:
            control_type = control.ControlTypeName
            name = (control.Name or "").strip()
        except Exception:
            continue
        if control_type in _KEEP_TYPES and _useful_name(name):
            try:
                accelerator = (control.AcceleratorKey or "").strip()
                access_key = (control.AccessKey or "").strip()
            except Exception:
                accelerator = access_key = ""
            if not accelerator:
                hinted = _NAME_SHORTCUT_RE.search(name)  # "Copy (Ctrl+C)"
                if hinted:
                    accelerator = hinted.group(1)
            previous = seen.get(name)
            if previous is None:
                element = UIElementInfo(name, control_type, accelerator, access_key)
                seen[name] = element
                info.elements.append(element)
            else:  # duplicate split-button halves: keep whichever carries keys
                previous.access_key = previous.access_key or access_key
                previous.accelerator = previous.accelerator or accelerator
        if depth >= max_depth or control_type in _SKIP_SUBTREES:
            continue
        children = _children(control)
        queue.extend((child, depth + 1) for child in children)
    info.elapsed_sec = round(time.monotonic() - started, 2)
    return info


# ---------------------------------------------------------------------------
# Building and writing the skill
# ---------------------------------------------------------------------------


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def build_skill(app: AppRecord, info: AppUIInfo, *, today: date | None = None) -> Skill:
    """Compose a ``generated`` skill from an inventory record and the UI reading."""
    today = today or date.today()
    title = app.name
    name = slugify(title)
    lines: list[str] = [f"# {title}", ""]

    lines += ["## What it's for", ""]
    about = f"{title}"
    if app.publisher:
        about += f" by {app.publisher}"
    if app.version:
        about += f" (version {app.version})"
    lines.append(
        about + ". This skill was generated automatically from the app's menus and "
        f"accessibility tree on {today.isoformat()}; check a Snapshot before relying on it."
    )
    lines.append("")

    lines += ["## Launch and deep links", ""]
    lines.append(f'- `App(mode="launch", name="{title}")`')
    if app.aumid:
        lines.append(
            f'- AUMID `{app.aumid}`: `PowerShell` -> `Start-Process "shell:AppsFolder\\{app.aumid}"`'
        )
    if app.exe:
        lines.append(f'- Executable `{app.exe}`: `App(mode="launch_executable", executable=...)`')
    for scheme in app.uri_schemes:
        lines.append(f'- URI scheme `{scheme}:` -> `PowerShell` -> `Start-Process "{scheme}:"`')
    if app.process_names:
        lines.append(f"- Process name(s): {', '.join(f'`{p}`' for p in app.process_names)}")
    lines.append("")

    shortcuts: list[tuple[str, str]] = []
    seen_shortcuts: set[str] = set()
    for items in info.menus.values():
        for item in items:
            if item.accelerator and item.path not in seen_shortcuts:
                seen_shortcuts.add(item.path)
                shortcuts.append((item.path, item.accelerator))
    for element in info.elements:
        if element.accelerator and element.name not in seen_shortcuts:
            seen_shortcuts.add(element.name)
            shortcuts.append((element.name, element.accelerator))
    lines += ["## Keyboard shortcuts", ""]
    if shortcuts:
        lines += ["| Command | Shortcut |", "|---|---|"]
        lines += [f"| {_md(cmd)} | `{_md(keys)}` |" for cmd, keys in shortcuts[:80]]
    else:
        lines.append("No accelerator keys were exposed through the menu bar or UI Automation.")
    lines.append("")

    if info.menus:
        lines += ["## Menus (access keys)", ""]
        for menu, items in info.menus.items():
            access = info.menu_access_keys.get(menu)
            header = f"**{menu}**" + (f" (`Alt+{access}`)" if access else "")
            entries = ", ".join(
                f"{item.name}" + (f" [{item.access_key}]" if item.access_key else "")
                for item in items[:30]
            )
            lines.append(f"- {header}: {entries}" if entries else f"- {header}")
        lines.append("")

    controls = [e for e in info.elements if e.control_type != "MenuItemControl" or not info.menus]
    keytips = [e for e in controls if is_keytip(e.access_key)]
    others = [e for e in controls if not is_keytip(e.access_key)]
    if keytips:
        lines += [
            "## Ribbon KeyTips",
            "",
            "Press the keys in order (Alt first). KeyTips are reported by UI Automation exactly as below.",
            "",
            "| Command | KeyTips |",
            "|---|---|",
        ]
        lines += [f"| {_md(e.name)} | `{_md(e.access_key)}` |" for e in keytips[:80]]
        lines.append("")
    if others:
        lines += ["## Toolbar and buttons", ""]
        for element in others[:60]:
            kind = element.control_type.removesuffix("Control")
            extra = []
            if element.access_key:
                extra.append(f"access key `{element.access_key}`")
            if element.accelerator:
                extra.append(f"shortcut `{element.accelerator}`")
            suffix = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"- {kind}: {_md(element.name)}{suffix}")
        lines.append("")

    lines += ["## Recipes", ""]
    lines.append(
        f'1. Open: `App(mode="launch", name="{title}")`, then `WaitFor(condition="active_window", window_name="{title}")`.'
    )
    if shortcuts:
        cmd, keys = next(((c, k) for c, k in shortcuts if "ctrl+" in k.lower()), shortcuts[0])
        lines.append(
            f'2. Run a command by shortcut, e.g. {cmd}: `Shortcut(shortcut="{keys.lower()}")`.'
        )
    simple = next((e for e in keytips if keytip_to_shortcuts(e.access_key)), None)
    if simple:
        calls = ", ".join(
            f'`Shortcut(shortcut="{k}")`' for k in keytip_to_shortcuts(simple.access_key)
        )
        lines.append(f"- Ribbon command by KeyTips, e.g. {simple.name}: {calls}.")
    if info.menus:
        menu, access = next(iter(info.menu_access_keys.items()), ("", ""))
        if access:
            lines.append(
                f'3. Open the {menu} menu with `Shortcut(shortcut="alt+{access.lower()}")`, then press the item\'s access key.'
            )
    lines.append(
        "- Anything else: `Snapshot` and `Click(label=...)` on the element names listed above."
    )
    lines.append("")

    lines += ["## UI notes", ""]
    lines.append(
        f"- Walked {info.visited} UI elements in {info.elapsed_sec}s"
        + (" (stopped early at the walk limit)." if info.truncated else ".")
    )
    if app.package_family:
        lines.append(
            "- Packaged (UWP/MSIX) app: its window frame belongs to `ApplicationFrameHost.exe`."
        )

    match = {
        "process": list(app.process_names),
        "aumid": [app.aumid] if app.aumid else [],
        "name": [title, f"* - {title}"],
        "exe": [Path(app.exe.replace("\\", "/")).name] if app.exe else [],
    }
    description = f"Auto-generated skill for {title}: launch options"
    description += ", keyboard shortcuts" if shortcuts else ""
    description += ", ribbon KeyTips" if keytips else ""
    description += " and UI elements."
    return Skill(
        name=name,
        title=title,
        description=description,
        version=1,
        source="generated",
        match=match,
        uri_schemes=list(app.uri_schemes),
        body="\n".join(lines),
    )


def write_generated(store: SkillStore, skill: Skill, *, regenerate: bool = False) -> Path:
    """Write *skill* into the user folder, refusing to touch non-generated files.

    Raises:
        GenerationError: If the target file exists and is a user skill, or exists
            as a generated skill and *regenerate* is false.
    """
    folder = store.user_dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{skill.name}.md"
    if path.exists():
        try:
            existing = Skill.parse(path.read_text(encoding="utf-8"), path=path)
        except Exception:
            existing = None
        if existing is None or existing.source != "generated":
            raise GenerationError(f"{path} is a user skill; refusing to overwrite it")
        if not regenerate:
            raise GenerationError(f"{path} already exists; pass regenerate=true to rebuild it")
        skill.version = existing.version + 1
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(skill.render(), encoding="utf-8")
    tmp.replace(path)
    skill.path = path
    store.reload()
    return path
