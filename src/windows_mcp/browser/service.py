"""Drive a Chromium tab over CDP: navigate, read, click, fill, press, eval, wait, screenshot.

All input goes through the renderer (``Input.dispatch*``), never the OS mouse/keyboard, so it
works on a background or covered window and never races the screen tools.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from typing import Any, Callable

from windows_mcp.browser import launcher, page_js
from windows_mcp.browser.cdp import CDPConnection, CDPDisconnected, CDPError

logger = logging.getLogger(__name__)

# After a click/Enter (and one renderer round trip): how long a navigation may take to start.
NAV_DETECT_S = 0.06
SEARCH_RESULTS_WAIT_S = 3.0
SEARCH_ENGINES = {
    "bing": "https://www.bing.com/search?q={q}",
    "google": "https://www.google.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
}
_SKIP_URL = ("devtools://", "chrome-extension://", "extension://")
_CONTEXT_GONE = ("context was destroyed", "cannot find context", "inspected target navigated")


class BrowserError(RuntimeError):
    """A user-facing failure (bad target, navigation error, ...)."""


def search_engine() -> str:
    raw = os.getenv("WINDOWS_MCP_BROWSER_SEARCH", "bing").strip().lower()
    return raw if raw in SEARCH_ENGINES else "bing"


# ---------------------------------------------------------------------- keys
_MODS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "win": 4, "cmd": 4, "shift": 8}
_MOD_KEYS = {
    1: ("Alt", "AltLeft", 18),
    2: ("Control", "ControlLeft", 17),
    4: ("Meta", "MetaLeft", 91),
    8: ("Shift", "ShiftLeft", 16),
}
_NAMED = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "return": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, None),
    "escape": ("Escape", "Escape", 27, None),
    "esc": ("Escape", "Escape", 27, None),
    "backspace": ("Backspace", "Backspace", 8, None),
    "delete": ("Delete", "Delete", 46, None),
    "del": ("Delete", "Delete", 46, None),
    "space": (" ", "Space", 32, " "),
    "arrowup": ("ArrowUp", "ArrowUp", 38, None),
    "up": ("ArrowUp", "ArrowUp", 38, None),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, None),
    "down": ("ArrowDown", "ArrowDown", 40, None),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, None),
    "left": ("ArrowLeft", "ArrowLeft", 37, None),
    "arrowright": ("ArrowRight", "ArrowRight", 39, None),
    "right": ("ArrowRight", "ArrowRight", 39, None),
    "home": ("Home", "Home", 36, None),
    "end": ("End", "End", 35, None),
    "pageup": ("PageUp", "PageUp", 33, None),
    "pagedown": ("PageDown", "PageDown", 34, None),
    "insert": ("Insert", "Insert", 45, None),
    **{f"f{i}": (f"F{i}", f"F{i}", 111 + i, None) for i in range(1, 13)},
}


def parse_chord(chord: str) -> tuple[int, tuple[str, str, int, str | None]]:
    """``"ctrl+shift+t"`` -> (modifier bitmask, (key, code, vk, text))."""
    parts = [p for p in re.split(r"\s*\+\s*", chord.strip()) if p]
    if not parts:
        raise BrowserError(f"empty key in {chord!r}")
    mods = 0
    for p in parts[:-1]:
        bit = _MODS.get(p.lower())
        if bit is None:
            raise BrowserError(f"unknown modifier {p!r} in {chord!r}")
        mods |= bit
    key = parts[-1]
    low = key.lower()
    if low in _MODS and len(parts) == 1:
        k, c, vk = _MOD_KEYS[_MODS[low]]
        return 0, (k, c, vk, None)
    if low in _NAMED:
        return mods, _NAMED[low]
    if len(key) == 1:
        ch = key.upper() if mods & 8 else key
        if ch.isalpha():
            code, vk = f"Key{ch.upper()}", ord(ch.upper())
        elif ch.isdigit():
            code, vk = f"Digit{ch}", ord(ch)
        else:
            code, vk = "", 0
        return mods, (ch, code, vk, ch)
    raise BrowserError(f"unknown key {key!r}")


def split_keys(keys: str | list[str]) -> list[str]:
    if isinstance(keys, list):
        return [k for k in keys if isinstance(k, str) and k.strip()]
    return [k for k in str(keys).split() if k]


def decode_bing(href: str) -> str:
    """Bing ``/ck/a?...&u=a1<base64url>`` redirect -> the real URL (unchanged otherwise)."""
    try:
        parsed = urllib.parse.urlparse(href)
        if "bing.com" not in parsed.netloc or not parsed.path.startswith("/ck/"):
            return href
        u = urllib.parse.parse_qs(parsed.query).get("u", [""])[0]
        if not u.startswith("a1"):
            return href
        raw = u[2:]
        raw += "=" * (-len(raw) % 4)
        real = base64.urlsafe_b64decode(raw).decode("utf-8")
        return real if real.startswith(("http://", "https://")) else href
    except Exception:
        return href


# ---------------------------------------------------------------------- service
class BrowserService:
    """One warm browser connection plus the tab being driven. Not thread-safe by itself;
    the tool layer serializes calls with :attr:`lock`."""

    def __init__(
        self,
        *,
        ensure: Callable[[str | None], tuple[dict, bool]] | None = None,
        connect: Callable[[str], CDPConnection] | None = None,
    ) -> None:
        self.lock = threading.Lock()
        self._ensure = ensure or launcher.ensure_browser
        self._connect = connect or (lambda url: CDPConnection(url))
        self.conn: CDPConnection | None = None
        self.product: str = ""
        self.target: str | None = None
        self.session: str | None = None
        self.frame: str | None = None
        self._sessions: dict[str, tuple[str, str]] = {}  # target -> (session, main frame)
        self.dialogs: list[dict] = []
        self.launched = False

    # ------------------------------------------------------------------ connection
    def _on_event(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Page.javascriptDialogOpening":
            accept = True
            self.dialogs.append(
                {"type": params.get("type"), "message": (params.get("message") or "")[:300]}
            )
            self.conn.send_nowait(
                "Page.handleJavaScriptDialog",
                {"accept": accept, "promptText": params.get("defaultPrompt") or ""},
                session=msg.get("sessionId"),
            )
        elif method == "Target.detachedFromTarget":
            sid = params.get("sessionId")
            for tid, (s, _) in list(self._sessions.items()):
                if s == sid:
                    self._sessions.pop(tid, None)
                    if tid == self.target:
                        self.target = self.session = self.frame = None
        elif method == "Target.targetDestroyed":
            tid = params.get("targetId")
            self._sessions.pop(tid, None)
            if tid == self.target:
                self.target = self.session = self.frame = None

    def connection(self) -> CDPConnection:
        """The live browser connection, attaching (or launching with *url*) when needed."""
        if self.conn is not None and not self.conn.closed:
            return self.conn
        self.reset()
        info, launched = self._ensure(None)
        self.launched = launched
        self.product = str(info.get("Browser") or "")
        conn = self._connect(info["webSocketDebuggerUrl"])
        conn.on_event(self._on_event)
        self.conn = conn
        conn.send("Target.setDiscoverTargets", {"discover": True})
        return conn

    def reset(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.conn = None
        self.target = self.session = self.frame = None
        self._sessions = {}

    def process_name(self) -> str:
        return "chrome.exe" if "chrome" in self.product.lower() else "msedge.exe"

    def pages(self) -> list[dict]:
        targets = self.connection().send("Target.getTargets").get("targetInfos") or []
        return [
            t
            for t in targets
            if t.get("type") == "page" and not str(t.get("url", "")).startswith(_SKIP_URL)
        ]

    def attach(self, target_id: str, *, activate: bool = False) -> str:
        conn = self.connection()
        if target_id in self._sessions:
            self.session, self.frame = self._sessions[target_id]
        else:
            sid = conn.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})[
                "sessionId"
            ]
            for method, params in (
                ("Page.enable", {}),
                ("Page.setLifecycleEventsEnabled", {"enabled": True}),
                ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ):
                try:
                    conn.send(method, params, session=sid)
                except CDPError:
                    logger.debug("%s failed", method, exc_info=True)
            frame = target_id
            try:
                tree = conn.send("Page.getFrameTree", session=sid)
                frame = tree["frameTree"]["frame"]["id"]
            except CDPError, KeyError:
                pass
            self._sessions[target_id] = (sid, frame)
            self.session, self.frame = sid, frame
        self.target = target_id
        if activate:
            try:
                conn.send("Target.activateTarget", {"targetId": target_id})
            except CDPError:
                pass
        return self.session

    def page(self) -> str:
        """Session id of the driven tab, picking (or creating) one when there is none."""
        conn = self.connection()
        if self.session and self.target and not conn.closed:
            return self.session
        pages = self.pages()
        if pages:
            return self.attach(pages[0]["targetId"])
        tid = conn.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        return self.attach(tid)

    # ------------------------------------------------------------------ evaluate
    def evaluate(self, expression: str, *, timeout: float = 10.0, retries: int = 2) -> Any:
        """Evaluate in the driven tab and return the JSON value; retries a torn-down context."""
        for attempt in range(retries + 1):
            sid = self.page()
            try:
                res = self.conn.send(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                        "userGesture": True,
                    },
                    session=sid,
                    timeout=timeout,
                )
            except CDPError as exc:
                if attempt < retries and any(s in str(exc).lower() for s in _CONTEXT_GONE):
                    time.sleep(0.05)
                    continue
                raise
            exc = res.get("exceptionDetails")
            if exc:
                text = (exc.get("exception") or {}).get("description") or exc.get("text") or ""
                if attempt < retries and any(s in text.lower() for s in _CONTEXT_GONE):
                    time.sleep(0.05)
                    continue
                raise BrowserError(f"JavaScript error: {str(text)[:500]}")
            return (res.get("result") or {}).get("value")
        return None

    def js(self, op: str, args: dict | None = None, *, timeout: float = 10.0) -> Any:
        return self.evaluate(page_js.call(op, args), timeout=timeout)

    def state(self) -> dict:
        try:
            return self.js("state", timeout=5.0) or {}
        except BrowserError, CDPError:
            return {}

    # ------------------------------------------------------------------ navigation waits
    def _is_main(self, msg: dict, *names: str) -> bool:
        return (
            msg.get("sessionId") == self.session
            and msg.get("method") in names
            and (msg.get("params") or {}).get("frameId", self.frame) == self.frame
        )

    def wait_loader(self, loader: str | None, since: int, wait_until: str, timeout: float) -> bool:
        """Wait for the lifecycle event of *loader* (DOMContentLoaded / load). True when seen."""
        if wait_until == "none" or not loader:
            return True
        name = "load" if wait_until == "load" else "DOMContentLoaded"
        sid = self.session

        def hit(msg: dict) -> bool:
            p = msg.get("params") or {}
            return (
                msg.get("sessionId") == sid
                and msg.get("method") == "Page.lifecycleEvent"
                and p.get("name") == name
                and p.get("loaderId") == loader
            )

        # Alternate short event waits with a readyState check: a back/forward-cache restore
        # or a document committed before we attached never fires the lifecycle event.
        deadline = time.monotonic() + timeout
        want = ("complete",) if wait_until == "load" else ("interactive", "complete")
        while True:
            remaining = deadline - time.monotonic()
            if self.conn.wait_event(hit, since=since, timeout=min(0.05, max(0.0, remaining))):
                return True
            if self.state().get("ready") in want:
                return True
            if time.monotonic() >= deadline:
                return False

    def settle(self, since: int, *, wait_until: str, timeout: float) -> dict:
        """After input: did it navigate (or open a tab)? If so wait for the new page."""
        out: dict[str, Any] = {}
        conn = self.conn
        opener = self.target

        def started(msg: dict) -> bool:
            method = msg.get("method")
            if method == "Target.targetCreated":
                info = (msg.get("params") or {}).get("targetInfo") or {}
                return info.get("type") == "page" and info.get("openerId") == opener
            return self._is_main(
                msg,
                "Page.frameRequestedNavigation",
                "Page.frameStartedLoading",
                "Page.frameNavigated",
                "Page.navigatedWithinDocument",
            )

        self.state()  # Round trip: the renderer has handled the input before we look.
        first = conn.wait_event(started, since=since, timeout=NAV_DETECT_S)
        if first is None:
            return out
        if first.get("method") == "Target.targetCreated":
            tid = first["params"]["targetInfo"]["targetId"]
            self.attach(tid, activate=True)
            out["new_tab"] = True
            out["navigated"] = True
            if wait_until != "none":
                out["loaded"] = self._ready_poll(
                    time.monotonic() + timeout, wait_until, skip_blank=True
                )
            return out
        elif first.get("method") == "Page.navigatedWithinDocument":
            out["navigated"] = True
            out["loaded"] = True
            return out
        else:
            frame_nav = first if first.get("method") == "Page.frameNavigated" else None
        out["navigated"] = True
        if wait_until == "none":
            return out
        deadline = time.monotonic() + timeout
        if frame_nav is None:
            frame_nav = conn.wait_event(
                lambda m: self._is_main(m, "Page.frameNavigated", "Page.navigatedWithinDocument"),
                since=since,
                timeout=timeout,
            )
        if frame_nav is None:
            out["loaded"] = self._ready_poll(deadline, wait_until)
            return out
        if frame_nav.get("method") == "Page.navigatedWithinDocument":
            out["loaded"] = True
            return out
        loader = ((frame_nav.get("params") or {}).get("frame") or {}).get("loaderId")
        out["loaded"] = self.wait_loader(
            loader, since, wait_until, max(0.0, deadline - time.monotonic())
        ) or self._ready_poll(deadline, wait_until)
        return out

    def _ready_poll(self, deadline: float, wait_until: str, *, skip_blank: bool = False) -> bool:
        want = ("complete",) if wait_until == "load" else ("interactive", "complete")
        while True:
            st = self.state()
            if st.get("ready") in want and not (
                skip_blank and st.get("url") in ("about:blank", "")
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    # ------------------------------------------------------------------ actions
    def goto(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout: float = 15.0,
        new_tab: bool = False,
    ) -> dict:
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
            url = "https://" + url
        conn = self.connection()
        if new_tab:
            tid = conn.send("Target.createTarget", {"url": "about:blank"})["targetId"]
            self.attach(tid, activate=True)
        sid = self.page()
        since = conn.mark()
        res = conn.send("Page.navigate", {"url": url}, session=sid, timeout=timeout)
        if res.get("errorText"):
            raise BrowserError(f"navigation to {url} failed: {res['errorText']}")
        loaded = self.wait_loader(res.get("loaderId"), since, wait_until, timeout)
        if not loaded:
            loaded = self._ready_poll(time.monotonic() + 0.5, wait_until)
        return {"navigated": True, "loaded": loaded}

    def history(self, step: int, *, wait_until: str, timeout: float) -> dict:
        sid = self.page()
        hist = self.conn.send("Page.getNavigationHistory", session=sid)
        idx = hist.get("currentIndex", 0) + step
        entries = hist.get("entries") or []
        if not 0 <= idx < len(entries):
            raise BrowserError("no page to go " + ("back to" if step < 0 else "forward to"))
        since = self.conn.mark()
        self.conn.send("Page.navigateToHistoryEntry", {"entryId": entries[idx]["id"]}, session=sid)
        return self._after_nav(since, wait_until, timeout)

    def reload(self, *, wait_until: str, timeout: float) -> dict:
        sid = self.page()
        since = self.conn.mark()
        self.conn.send("Page.reload", {}, session=sid)
        return self._after_nav(since, wait_until, timeout)

    def _after_nav(self, since: int, wait_until: str, timeout: float) -> dict:
        nav = self.conn.wait_event(
            lambda m: self._is_main(m, "Page.frameNavigated", "Page.navigatedWithinDocument"),
            since=since,
            timeout=timeout,
        )
        if nav is None:
            return {"navigated": False, "loaded": False}
        if nav.get("method") == "Page.navigatedWithinDocument":
            return {"navigated": True, "loaded": True}
        loader = ((nav.get("params") or {}).get("frame") or {}).get("loaderId")
        return {"navigated": True, "loaded": self.wait_loader(loader, since, wait_until, timeout)}

    def search_results(self, engine: str, max_results: int = 8) -> dict:
        deadline = time.monotonic() + SEARCH_RESULTS_WAIT_S
        while True:
            res = self.js("results", {"engine": engine, "max": max_results}) or {}
            if res.get("results") or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        for r in res.get("results") or []:
            r["href"] = decode_bing(r.get("href") or "")
        return res

    def read(self, *, max_chars: int = 3000, max_elements: int = 60) -> dict:
        return self.js("read", {"max": max_elements, "maxText": max_chars}) or {}

    def resolve(self, spec: dict, kind: str) -> dict:
        res = self.js("resolve", {**spec, "kind": kind}) or {}
        if res.get("error"):
            msg = res.get("message") or res["error"]
            cands = res.get("candidates")
            if cands:
                msg += "; candidates: " + "; ".join(cands)
            raise BrowserError(msg)
        return res

    def mouse_click(self, x: float, y: float, clicks: int = 1, button: str = "left") -> None:
        sid = self.page()
        send = self.conn.send
        send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session=sid)
        for i in range(1, clicks + 1):
            base = {"x": x, "y": y, "button": button, "clickCount": i}
            send(
                "Input.dispatchMouseEvent",
                {**base, "type": "mousePressed", "buttons": 1},
                session=sid,
            )
            send(
                "Input.dispatchMouseEvent",
                {**base, "type": "mouseReleased", "buttons": 0},
                session=sid,
            )

    def click(self, el: dict, *, clicks: int = 1, button: str = "left") -> str:
        if el.get("hit"):
            self.mouse_click(el["x"], el["y"], clicks, button)
            return "mouse"
        self.js("jsclick", {"n": el["n"]})
        return "js"

    def insert_text(self, text: str) -> None:
        if text:
            self.conn.send("Input.insertText", {"text": text}, session=self.page())

    def key(self, chord: str) -> None:
        mods, (key, code, vk, text) = parse_chord(chord)
        sid = self.page()
        send = self.conn.send
        held = 0
        for bit in (2, 1, 4, 8):
            if mods & bit:
                held |= bit
                k, c, v = _MOD_KEYS[bit]
                send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "rawKeyDown",
                        "key": k,
                        "code": c,
                        "windowsVirtualKeyCode": v,
                        "modifiers": held,
                    },
                    session=sid,
                )
        typed = text if not (mods & (1 | 2 | 4)) else None
        down = {
            "type": "keyDown" if typed else "rawKeyDown",
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": mods,
        }
        if typed:
            down["text"] = down["unmodifiedText"] = typed
        send("Input.dispatchKeyEvent", down, session=sid)
        send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
                "modifiers": mods,
            },
            session=sid,
        )
        for bit in (8, 4, 1, 2):
            if mods & bit:
                held &= ~bit
                k, c, v = _MOD_KEYS[bit]
                send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyUp",
                        "key": k,
                        "code": c,
                        "windowsVirtualKeyCode": v,
                        "modifiers": held,
                    },
                    session=sid,
                )

    def wait(self, cond: dict, timeout: float) -> dict:
        """Return as soon as *cond* holds in the page (retrying across navigations)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                res = self.js(
                    "wait",
                    {**cond, "timeout": max(0.0, remaining) * 1000},
                    timeout=remaining + 3.0,
                )
            except BrowserError, CDPError:
                res = None
            if isinstance(res, dict) and res.get("ok"):
                return res
            if time.monotonic() >= deadline:
                return res if isinstance(res, dict) else {"ok": False}
            time.sleep(0.05)

    def screenshot(self, *, full_page: bool = False, quality: int = 70) -> bytes:
        sid = self.page()
        try:  # A hidden (background) tab never paints, so the capture would hang.
            self.conn.send("Page.bringToFront", session=sid, timeout=3.0)
        except CDPError:
            pass
        params: dict[str, Any] = {"format": "jpeg", "quality": quality}
        if full_page:
            params["captureBeyondViewport"] = True
            metrics = self.conn.send("Page.getLayoutMetrics", session=sid)
            size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            if size:
                params["clip"] = {
                    "x": 0,
                    "y": 0,
                    "scale": 1,
                    "width": size.get("width", 1280),
                    "height": min(size.get("height", 800), 8000),
                }
        data = self.conn.send("Page.captureScreenshot", params, session=sid, timeout=15.0)
        return base64.b64decode(data.get("data") or "")

    # ------------------------------------------------------------------ tabs
    def tabs(self) -> list[dict]:
        return [
            {
                "tab": i + 1,
                "title": t.get("title", ""),
                "url": t.get("url", ""),
                "active": t.get("targetId") == self.target,
                "id": t.get("targetId"),
            }
            for i, t in enumerate(self.pages())
        ]

    def find_tab(self, tab: int | str) -> dict:
        tabs = self.tabs()
        if isinstance(tab, int) or (isinstance(tab, str) and tab.strip().isdigit()):
            i = int(tab) - 1
            if 0 <= i < len(tabs):
                return tabs[i]
            raise BrowserError(f"no tab {tab}; there are {len(tabs)}")
        want = str(tab).lower()
        for t in tabs:
            if t["id"] == tab or want in t["title"].lower() or want in t["url"].lower():
                return t
        raise BrowserError(f"no tab matching {tab!r}")

    def new_tab(self, url: str | None, *, wait_until: str, timeout: float) -> dict:
        conn = self.connection()
        tid = conn.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.attach(tid, activate=True)
        if url:
            return self.goto(url, wait_until=wait_until, timeout=timeout)
        return {"loaded": True}

    def close_tab(self, tab: int | str | None) -> None:
        tid = self.find_tab(tab)["id"] if tab is not None else self.target
        if not tid:
            raise BrowserError("no tab to close")
        self.connection().send("Target.closeTarget", {"targetId": tid})
        self._sessions.pop(tid, None)
        if tid == self.target:
            self.target = self.session = self.frame = None
            rest = self.pages()
            if rest:
                self.attach(rest[0]["targetId"], activate=True)

    def take_dialogs(self) -> list[dict]:
        out, self.dialogs = self.dialogs, []
        return out


_service: BrowserService | None = None
_service_lock = threading.Lock()


def get_service() -> BrowserService:
    global _service
    with _service_lock:
        if _service is None:
            _service = BrowserService()
        return _service


def set_service(service: BrowserService | None) -> None:
    """Replace the singleton (tests)."""
    global _service
    with _service_lock:
        _service = service


def is_disconnect(exc: BaseException) -> bool:
    return isinstance(exc, CDPDisconnected)


def dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
