"""Read "where is the user" from the desktop: foreground window, browser URL, focused field,
email recipients and (only when a page is not known-safe) a clipped excerpt of its text.

Windows only; every Windows import is lazy so the package stays importable elsewhere. The
probe works on a raw ``IUIAutomation`` pointer that the caller owns: the shared client on the
UIA worker for the ``Context`` tool, or the watcher thread's own client. Cost is kept low by
caching the address-bar element per window, so a repeat read is one ``ValueValue`` call.
"""

from __future__ import annotations

import ctypes
import logging
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# UIA ids (uia/enums.py) inlined so this module does not need the wrapper's singleton.
TREE_SCOPE_DESCENDANTS = 4
TREE_SCOPE_SUBTREE = 7
UIA_EDIT = 50004
UIA_COMBOBOX = 50003
UIA_DOCUMENT = 50030
UIA_TEXT = 50020
PROP_NAME = 30005
PROP_CONTROL_TYPE = 30003
PROP_CLASS_NAME = 30012
PROP_AUTOMATION_ID = 30011
PROP_VALUE = 30045
PATTERN_TEXT = 10014

BROWSERS = {
    "msedge.exe": "edge",
    "chrome.exe": "chrome",
    "brave.exe": "brave",
    "firefox.exe": "firefox",
    "opera.exe": "opera",
    "vivaldi.exe": "vivaldi",
    "arc.exe": "arc",
}
EMAIL_APPS = {"outlook.exe": "outlook", "olk.exe": "outlook-new", "hxoutlook.exe": "mail"}
WEBMAIL = {
    "mail.google.com",
    "outlook.live.com",
    "outlook.office.com",
    "outlook.office365.com",
    "mail.yahoo.com",
    "mail.proton.me",
    "mail.zoho.com",
    "mail.aol.com",
    "app.fastmail.com",
}
ADDRESS_BAR_NAMES = ("Address and search bar", "Search or enter address", "Address field")
ADDRESS_BAR_CLASSES = ("OmniboxViewViews",)
ADDRESS_BAR_IDS = ("urlbar-input",)
RECIPIENT_FIELD_NAMES = (
    "To",
    "Cc",
    "Bcc",
    "To:",
    "Cc:",
    "Bcc:",
    "To recipients",
    "Cc recipients",
    "Bcc recipients",
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z¡-￿]{2,}")
MAX_TEXT = 6000
MAX_RECIPIENT_NODES = 200

_user32 = None
_pid_names: dict[int, str] = {}


def _u32():
    global _user32
    if _user32 is None:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetAncestor.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        _user32 = u
    return _user32


def foreground() -> tuple[int, str, int]:
    """(root hwnd, title, pid) of the foreground window; (0, "", 0) when there is none.

    Pure Win32, a few microseconds: the gate calls it on every guarded tool call.
    """
    try:
        u = _u32()
        hwnd = u.GetForegroundWindow() or 0
        if not hwnd:
            return 0, "", 0
        hwnd = u.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        pid = ctypes.c_ulong(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(hwnd), buf.value, int(pid.value)
    except Exception:
        return 0, "", 0


def process_name(pid: int) -> str:
    if not pid:
        return ""
    name = _pid_names.get(pid)
    if name is None:
        try:
            import psutil

            name = psutil.Process(pid).name().lower()
        except Exception:
            name = ""
        if len(_pid_names) > 512:
            _pid_names.clear()
        _pid_names[pid] = name
    return name


def classify_app(process: str) -> tuple[str, str | None]:
    """("browser"|"email"|"other", product)."""
    if process in BROWSERS:
        return "browser", BROWSERS[process]
    if process in EMAIL_APPS:
        return "email", EMAIL_APPS[process]
    return "other", None


def _as_url(value: str) -> str | None:
    value = (value or "").strip()
    if not value or " " in value or "." not in value and "://" not in value:
        return None
    return value


class Prober:
    """Stateful probe bound to one IUIAutomation client (one per thread)."""

    def __init__(self, client: Any, iface_module: Any = None) -> None:
        self.client = client
        self.mod = iface_module
        self._address: dict[int, Any] = {}  # hwnd -> address bar element
        self._committed: dict[int, str] = {}  # hwnd -> last URL read while the bar was unfocused
        self._address_cond = None
        self._recipient_cond = None
        self._document_cond = None
        self._lock = threading.Lock()
        self.text_via: str | None = None

    # ------------------------------------------------------------------ conditions
    def _or(self, conds: list) -> Any:
        out = conds[0]
        for c in conds[1:]:
            out = self.client.CreateOrCondition(out, c)
        return out

    def _address_condition(self) -> Any:
        if self._address_cond is None:
            c = self.client
            conds = [c.CreatePropertyCondition(PROP_CLASS_NAME, n) for n in ADDRESS_BAR_CLASSES]
            conds += [c.CreatePropertyCondition(PROP_AUTOMATION_ID, n) for n in ADDRESS_BAR_IDS]
            conds += [c.CreatePropertyCondition(PROP_NAME, n) for n in ADDRESS_BAR_NAMES]
            self._address_cond = c.CreateAndCondition(
                c.CreatePropertyCondition(PROP_CONTROL_TYPE, UIA_EDIT), self._or(conds)
            )
        return self._address_cond

    def _recipient_condition(self) -> Any:
        if self._recipient_cond is None:
            c = self.client
            self._recipient_cond = self._or(
                [c.CreatePropertyCondition(PROP_NAME, n) for n in RECIPIENT_FIELD_NAMES]
            )
        return self._recipient_cond

    def _document_condition(self) -> Any:
        if self._document_cond is None:
            self._document_cond = self.client.CreatePropertyCondition(
                PROP_CONTROL_TYPE, UIA_DOCUMENT
            )
        return self._document_cond

    # ------------------------------------------------------------------ pieces
    def focused(self, pid: int) -> dict:
        try:
            el = self.client.GetFocusedElement()
        except Exception:
            return {}
        if not el:
            return {}
        out: dict[str, Any] = {}
        try:
            if pid and el.CurrentProcessId != pid:
                return {"outside_foreground": True}
            out["name"] = (el.CurrentName or "")[:120]
            out["control_type"] = _CONTROL_TYPES.get(el.CurrentControlType, "Other")
            out["is_password"] = bool(el.CurrentIsPassword)
            out["address_bar"] = (el.CurrentClassName or "") in ADDRESS_BAR_CLASSES or (
                el.CurrentAutomationId or ""
            ) in ADDRESS_BAR_IDS
        except Exception:
            pass
        return out

    def address_bar(self, hwnd: int) -> Any:
        el = self._address.get(hwnd)
        if el is not None:
            return el
        root = self.client.ElementFromHandle(hwnd)
        if not root:
            return None
        el = root.FindFirst(TREE_SCOPE_DESCENDANTS, self._address_condition())
        if el:
            if len(self._address) > 64:
                self._address.clear()
            self._address[hwnd] = el
        return el or None

    def url(self, hwnd: int, bar_focused: bool) -> str | None:
        for attempt in range(2):
            el = self.address_bar(hwnd)
            if el is None:
                return self._committed.get(hwnd)
            try:
                value = el.GetCurrentPropertyValue(PROP_VALUE)
                break
            except Exception:
                self._address.pop(hwnd, None)  # Stale element (window rebuilt); retry once.
                if attempt:
                    return self._committed.get(hwnd)
        url = _as_url(value if isinstance(value, str) else "")
        if bar_focused:
            # While the user types, the bar holds their text, not the page URL.
            return self._committed.get(hwnd) or url
        if url:
            self._committed[hwnd] = url
        return url

    def recipients(self, hwnd: int, budget_s: float = 0.15) -> list[str]:
        deadline = time.perf_counter() + budget_s
        root = self.client.ElementFromHandle(hwnd)
        if not root:
            return []
        fields = root.FindAll(TREE_SCOPE_DESCENDANTS, self._recipient_condition())
        found: list[str] = []
        n = fields.Length if fields else 0
        true_cond = self.client.CreateTrueCondition()
        for i in range(min(n, 6)):
            if time.perf_counter() > deadline:
                break
            field = fields.GetElement(i)
            texts = []
            try:
                value = field.GetCurrentPropertyValue(PROP_VALUE)
                if isinstance(value, str):
                    texts.append(value)
            except Exception:
                pass
            # Recipient "chips" (new Outlook, OWA, Gmail) live next to / inside the field.
            scope_el = field
            try:
                parent = self.client.ControlViewWalker.GetParentElement(field)
                if parent:
                    scope_el = parent
            except Exception:
                pass
            try:
                kids = scope_el.FindAll(TREE_SCOPE_SUBTREE, true_cond)
                for j in range(min(kids.Length if kids else 0, MAX_RECIPIENT_NODES)):
                    k = kids.GetElement(j)
                    texts.append(k.CurrentName or "")
            except Exception:
                pass
            for text in texts:
                for addr in EMAIL_RE.findall(text):
                    addr = addr.lower().strip(".")
                    if addr not in found:
                        found.append(addr)
        return found[:20]

    def page_text(self, hwnd: int, limit: int = MAX_TEXT) -> str | None:
        root = self.client.ElementFromHandle(hwnd)
        if not root:
            return None
        doc = root.FindFirst(TREE_SCOPE_DESCENDANTS, self._document_condition())
        if not doc:
            return None
        try:
            mod = self.mod or _iface_module()
            raw = doc.GetCurrentPattern(PATTERN_TEXT)
            if raw:
                pattern = raw.QueryInterface(mod.IUIAutomationTextPattern)
                text = pattern.DocumentRange.GetText(limit)
                if text and len(text.strip()) > 40:
                    self.text_via = "textpattern"
                    return text
        except Exception as exc:
            logger.debug("guard: TextPattern read failed: %s", exc)
        # Fallback: names of the text nodes under the document (bounded).
        try:
            cond = self.client.CreatePropertyCondition(PROP_CONTROL_TYPE, UIA_TEXT)
            nodes = doc.FindAll(TREE_SCOPE_DESCENDANTS, cond)
            parts, total = [], 0
            for i in range(min(nodes.Length if nodes else 0, 300)):
                name = nodes.GetElement(i).CurrentName or ""
                if name:
                    parts.append(name)
                    total += len(name) + 1
                    if total >= limit:
                        break
            if parts:
                self.text_via = "text-nodes"
                return " ".join(parts)[:limit]
        except Exception as exc:
            logger.debug("guard: text-node read failed: %s", exc)
        try:
            self.text_via = "document-name"
            return (doc.CurrentName or "")[:limit] or None
        except Exception:
            return None

    # ------------------------------------------------------------------ full probe
    def probe(
        self,
        *,
        text: str = "auto",
        recipients: str = "auto",
        fg: tuple[int, str, int] | None = None,
    ) -> dict:
        """Read the current context. ``text``/``recipients``: "auto" | "always" | "never"."""
        start = time.perf_counter()
        hwnd, title, pid = fg or foreground()
        process = process_name(pid)
        app, product = classify_app(process)
        ctx: dict[str, Any] = {
            "hwnd": hwnd,
            "title": title,
            "process": process,
            "app": app,
            "product": product,
            "url": None,
            "domain": None,
            "focused": {},
            "recipients": [],
            "page_text": None,
        }
        timings: dict[str, float] = {}
        if not hwnd:
            ctx["ts"] = time.time()
            ctx["probe_ms"] = round((time.perf_counter() - start) * 1000, 2)
            return ctx
        with self._lock:
            t = time.perf_counter()
            ctx["focused"] = self.focused(pid)
            timings["focus"] = (time.perf_counter() - t) * 1000
            safe = False
            if app == "browser":
                t = time.perf_counter()
                try:
                    ctx["url"] = self.url(hwnd, bool(ctx["focused"].get("address_bar")))
                except Exception as exc:
                    logger.debug("guard: address bar read failed: %s", exc)
                timings["url"] = (time.perf_counter() - t) * 1000
                if ctx["url"]:
                    from windows_mcp.guard.detector import check
                    from windows_mcp.guard.domains import parse_target

                    target = parse_target(ctx["url"])
                    if target.host:
                        ctx["domain"] = target.host.domain
                    quick = check(target)
                    safe = quick.verdict == "safe"
            webmail = ctx["domain"] in WEBMAIL or (
                ctx["url"] is not None and any(w in ctx["url"] for w in WEBMAIL)
            )
            if recipients == "always" or (recipients == "auto" and (app == "email" or webmail)):
                t = time.perf_counter()
                try:
                    ctx["recipients"] = self.recipients(hwnd)
                except Exception as exc:
                    logger.debug("guard: recipient read failed: %s", exc)
                timings["recipients"] = (time.perf_counter() - t) * 1000
                if ctx["recipients"] and app == "browser":
                    ctx["app"] = "email"
            if (
                app == "browser"
                and ctx["url"]
                and (text == "always" or (text == "auto" and not safe))
            ):
                t = time.perf_counter()
                try:
                    ctx["page_text"] = self.page_text(hwnd)
                except Exception as exc:
                    logger.debug("guard: page text read failed: %s", exc)
                timings["text"] = (time.perf_counter() - t) * 1000
                ctx["text_via"] = self.text_via
        ctx["ts"] = time.time()
        ctx["probe_ms"] = round((time.perf_counter() - start) * 1000, 2)
        ctx["timings_ms"] = {k: round(v, 2) for k, v in timings.items()}
        return ctx


def _iface_module() -> Any:
    from windows_mcp.uia.core import _AutomationClient

    return _AutomationClient.instance().UIAutomationCore


_CONTROL_TYPES = {
    50000: "Button",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50007: "ListItem",
    50011: "MenuItem",
    50013: "RadioButton",
    50020: "Text",
    50025: "Custom",
    50026: "Group",
    50030: "Document",
    50032: "Window",
    50033: "Pane",
}

_shared: Prober | None = None


def shared_prober() -> Prober:
    """Prober on the shared UIA client. Call only on the UIA worker thread."""
    global _shared
    if _shared is None:
        from windows_mcp.uia.core import _AutomationClient

        inst = _AutomationClient.instance()
        _shared = Prober(inst.IUIAutomation, inst.UIAutomationCore)
    return _shared
