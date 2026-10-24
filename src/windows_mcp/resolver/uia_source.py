"""Tier-1 candidate source: one cached UIA FindAll per window, nothing per element.

``Control.FindAllBuildCache`` wraps every hit with ``CreateControlFromElement``,
which reads ``CurrentControlType`` live, i.e. one cross-process round trip per
element. Here the raw ``IUIAutomationElement.FindAllBuildCache`` is called with a
slim cache request and only ``Cached*`` properties are read afterwards, so a whole
window costs a single provider round trip plus Python-side iteration.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from time import sleep

from windows_mcp.resolver.scoring import Candidate
from windows_mcp.tree.budget import resolve_max_tree_elements

logger = logging.getLogger(__name__)

GA_ROOT = 2
GW_OWNER = 4
MENU_CLASS = "#32768"
CHROMIUM_CLASS = "Chrome_WidgetWin_1"
CHROMIUM_RETRY_DELAY = 0.25
CHROMIUM_MIN_ELEMENTS = 5

# Types fetched in the first, cheap pass. Everything else (text, images, groups,
# custom controls) is fetched only when the first pass finds nothing convincing.
PRIMARY_TYPE_NAMES = (
    "ButtonControl",
    "SplitButtonControl",
    "MenuItemControl",
    "TabItemControl",
    "HyperlinkControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "ComboBoxControl",
    "EditControl",
    "ListItemControl",
    "TreeItemControl",
    "DataItemControl",
    "HeaderItemControl",
    "SliderControl",
    "SpinnerControl",
    "DocumentControl",
)


@dataclass
class TargetWindow:
    """A top-level window to search, with whether it is the foreground one."""

    handle: int
    title: str
    class_name: str
    process_id: int
    in_foreground: bool = True


def _win32():
    import win32gui
    import win32process

    return win32gui, win32process


def _describe(handle: int, in_foreground: bool) -> TargetWindow:
    win32gui, win32process = _win32()
    try:
        title = win32gui.GetWindowText(handle) or ""
    except Exception:
        title = ""
    try:
        class_name = win32gui.GetClassName(handle) or ""
    except Exception:
        class_name = ""
    try:
        pid = win32process.GetWindowThreadProcessId(handle)[1]
    except Exception:
        pid = 0
    return TargetWindow(handle, title, class_name, pid, in_foreground)


def foreground_window() -> TargetWindow | None:
    """The root window that currently has the foreground."""
    win32gui, _ = _win32()
    handle = win32gui.GetForegroundWindow()
    if not handle:
        return None
    try:
        handle = win32gui.GetAncestor(handle, GA_ROOT) or handle
    except Exception:
        pass
    return _describe(handle, True)


def _visible_top_level() -> list[int]:
    win32gui, _ = _win32()
    handles: list[int] = []

    def callback(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd):
                handles.append(hwnd)
        except Exception:
            pass

    win32gui.EnumWindows(callback, None)
    return handles


def popup_windows(root: TargetWindow) -> list[TargetWindow]:
    """Open menus and owned popups that belong to ``root``.

    Win32 drop-down menus (class ``#32768``) and owned dialogs are separate
    top-level windows, so a search scoped to the main window's subtree never sees
    "Save As..." after "File" was clicked.
    """
    win32gui, win32process = _win32()
    own_pid = os.getpid()
    found: list[TargetWindow] = []
    for hwnd in _visible_top_level():
        if hwnd == root.handle:
            continue
        try:
            class_name = win32gui.GetClassName(hwnd)
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            owner = win32gui.GetWindow(hwnd, GW_OWNER)
        except Exception:
            continue
        if pid == own_pid:
            continue
        if class_name == MENU_CLASS or owner == root.handle:
            found.append(_describe(hwnd, True))
    return found


def find_window(name: str) -> TargetWindow | None:
    """Fuzzy-find a visible top-level window by title or process name."""
    from thefuzz import fuzz

    win32gui, win32process = _win32()
    own_pid = os.getpid()
    query = name.strip().casefold()
    if query.endswith(".exe"):
        query = query[:-4]
    best: tuple[int, int] | None = None
    for hwnd in _visible_top_level():
        try:
            title = win32gui.GetWindowText(hwnd) or ""
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        except Exception:
            continue
        if not title or pid == own_pid:
            continue
        score = fuzz.WRatio(query, title.casefold())
        if query in title.casefold():
            score = max(score, 95)
        try:
            import psutil

            exe = psutil.Process(pid).name().casefold().removesuffix(".exe")
            if exe == query:
                score = max(score, 100)
            elif query in exe:
                score = max(score, 85)
        except Exception:
            pass
        if score >= 70 and (best is None or score > best[0]):
            best = (score, hwnd)
    if best is None:
        return None
    foreground = foreground_window()
    is_fg = foreground is not None and foreground.handle == best[1]
    return _describe(best[1], is_fg)


class _Requests:
    """Lazily built, reused UIA cache request and conditions."""

    def __init__(self) -> None:
        import windows_mcp.uia as uia

        self.uia = uia
        req = uia.CacheRequest()
        req.TreeScope = uia.TreeScope.TreeScope_Element
        try:
            req.AutomationElementMode = 0  # AutomationElementMode_None: cached data only
        except Exception:
            pass
        for prop in (
            uia.PropertyId.NameProperty,
            uia.PropertyId.ControlTypeProperty,
            uia.PropertyId.BoundingRectangleProperty,
            uia.PropertyId.AutomationIdProperty,
            uia.PropertyId.IsEnabledProperty,
            uia.PropertyId.HasKeyboardFocusProperty,
            uia.PropertyId.HelpTextProperty,
            uia.PropertyId.LegacyIAccessibleNameProperty,
        ):
            req.AddProperty(prop)
        self.request = req
        visible = uia.CreateAndCondition(
            uia.CreatePropertyCondition(uia.PropertyId.IsControlElementProperty, True),
            uia.CreatePropertyCondition(uia.PropertyId.IsOffscreenProperty, False),
        )
        types = None
        for type_name in PRIMARY_TYPE_NAMES:
            cond = uia.CreatePropertyCondition(
                uia.PropertyId.ControlTypeProperty, getattr(uia.ControlType, type_name)
            )
            types = cond if types is None else uia.CreateOrCondition(types, cond)
        self.primary = uia.CreateAndCondition(visible, types)
        self.secondary = uia.CreateAndCondition(visible, uia.CreateNotCondition(types))


_requests: _Requests | None = None


def _get_requests() -> _Requests:
    global _requests
    if _requests is None:
        _requests = _Requests()
    return _requests


def _read(element, window: TargetWindow, type_names: dict[int, str]) -> Candidate | None:
    rect = element.CachedBoundingRectangle
    left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    if right <= left or bottom <= top:
        return None
    reqs = _get_requests()
    try:
        legacy = element.GetCachedPropertyValue(reqs.uia.PropertyId.LegacyIAccessibleNameProperty)
    except Exception:
        legacy = ""
    return Candidate(
        name=element.CachedName or "",
        control_type=type_names.get(element.CachedControlType, "Unknown"),
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        automation_id=element.CachedAutomationId or "",
        help_text=element.CachedHelpText or "",
        legacy_name=legacy if isinstance(legacy, str) else "",
        enabled=bool(element.CachedIsEnabled),
        focused=bool(element.CachedHasKeyboardFocus),
        in_foreground=window.in_foreground,
        window_title=window.title,
        window_handle=window.handle,
    )


def collect(window: TargetWindow, *, secondary: bool = False) -> list[Candidate]:
    """Return the on-screen control elements of ``window``.

    ``secondary=False`` fetches clickable control types; ``True`` fetches the rest.
    Raises the UIA timeout error unchanged so the caller can report it.
    """
    reqs = _get_requests()
    uia = reqs.uia
    limit = max(2000, resolve_max_tree_elements() * 4)
    root = uia.ControlFromHandle(window.handle)
    if root is None:
        return []
    condition = reqs.secondary if secondary else reqs.primary
    attempts = 2 if window.class_name == CHROMIUM_CLASS and not secondary else 1
    out: list[Candidate] = []
    for attempt in range(attempts):
        array = root.Element.FindAllBuildCache(
            uia.TreeScope.TreeScope_Descendants, condition, reqs.request.check_request
        )
        out = []
        length = array.Length if array else 0
        for i in range(min(length, limit)):
            try:
                cand = _read(array.GetElement(i), window, uia.ControlTypeNames)
            except Exception as exc:  # dead element mid-read
                logger.debug("resolver: skipping element %d: %s", i, exc)
                continue
            if cand is not None:
                out.append(cand)
        # Chromium builds its accessibility tree lazily on the first UIA query.
        if len(out) >= CHROMIUM_MIN_ELEMENTS or attempt + 1 >= attempts:
            break
        sleep(CHROMIUM_RETRY_DELAY)
    return out


def virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the virtual desktop."""
    import windows_mcp.uia as uia

    return uia.GetVirtualScreenRect()
