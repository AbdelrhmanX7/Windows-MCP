"""Browser tool: drive Edge/Chrome web pages over the Chrome DevTools Protocol (no screen input)."""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations

from windows_mcp.desktop.utils import as_bool as _as_bool
from windows_mcp.infrastructure import with_analytics

MODES = (
    "open",
    "search",
    "tabs",
    "read",
    "click",
    "fill",
    "type",
    "press",
    "eval",
    "wait",
    "screenshot",
    "back",
    "forward",
    "reload",
)
GATED_MODES = frozenset({"click", "fill", "type", "press", "eval"})

DESCRIPTION = (
    "Fast web automation inside Edge/Chrome over the DevTools protocol: no screenshots, no "
    "mouse/keyboard, works with the browser in the background (safe to run alongside screen "
    "tools). Prefer it over Click/Type/Snapshot for anything inside a web page. Attaches to the "
    "browser on the debugging port or launches Edge with a persistent profile. Every action "
    "returns the new url/title (and loaded=true after a navigation) so no extra Wait is needed. "
    "Modes: open(url, new_tab?) | search(query, engine?) -> top results, numbered | "
    "read -> page text + numbered interactive elements | click(element|target|selector|role+"
    "name, nth?) | fill(element|target|selector, text or list of texts, submit?) replaces the "
    "value | type(text, target?, submit?) appends at the caret / focused field | press(keys, "
    "e.g. 'Enter', 'ctrl+a', 'Tab Tab Enter') | eval(script) -> JSON | wait(text|selector|url, "
    "gone?, timeout) returns as soon as true | screenshot(full_page?) page JPEG | "
    "tabs(action=list|switch|new|close, tab=number or text, url?) | back | forward | reload. "
    "element numbers come from the last read/search. read=true on any action appends the read. "
    "On a page the scam guard flags, click/fill/type/press Enter/eval are refused with "
    "SCAM_GUARD_CONFIRM_REQUIRED until the user approves (then retry with confirmed=true)."
)


def _guard():
    from windows_mcp.guard.service import get_service, guard_enabled

    return get_service() if guard_enabled() else None


def _http(url: str | None) -> bool:
    return bool(url) and str(url).lower().startswith(("http://", "https://"))


def _verdict_summary(verdict) -> dict | None:
    if verdict is None or verdict.verdict not in ("suspicious", "dangerous"):
        return None
    return {
        "verdict": verdict.verdict,
        "score": verdict.score,
        "domain": verdict.domain,
        "brand": verdict.brand,
        "reasons": list(verdict.reasons)[:3],
    }


def guard_page(svc, url: str | None, title: str | None, password: bool) -> dict | None:
    """Verdict for the page after a navigation (never blocks; emits security_events)."""
    guard = _guard()
    if guard is None or not _http(url):
        return None
    try:
        verdict = guard.observe_tab(url, title=title, password=password, process=svc.process_name())
    except Exception:
        return None
    return _verdict_summary(verdict)


def guard_input(svc, url: str | None, title: str | None, password: bool, confirmed: bool) -> None:
    """Refuse input into a risky page unless confirmed (then record the override)."""
    guard = _guard()
    if guard is None or not _http(url):
        return
    from windows_mcp.guard.service import GuardService

    try:
        verdict = guard.observe_tab(url, title=title, password=password, process=svc.process_name())
    except Exception:
        return  # A detector failure must not break web automation.
    risk = GuardService.risk_payload(verdict, source="browser", password=password)
    if risk is None:
        return
    if confirmed:
        guard.publish_override("Browser", risk)
        return
    raise ToolError(json.dumps(risk, ensure_ascii=False))


def _risky_keys(keys: list[str]) -> bool:
    from windows_mcp.guard.service import _RISKY_NORMALIZED, normalize_shortcut

    return any(normalize_shortcut(k) in _RISKY_NORMALIZED for k in keys)


def _short_href(href: str, page: str) -> str:
    try:
        a, b = urllib.parse.urlsplit(href), urllib.parse.urlsplit(page)
        if a.scheme in ("http", "https") and a.netloc == b.netloc:
            href = a.path + (f"?{a.query}" if a.query else "") or "/"
    except ValueError:
        pass
    return href if len(href) <= 100 else href[:99] + "…"


def format_element(el: dict, page_url: str) -> str:
    line = f'[{el.get("n")}] {el.get("role")} "{el.get("label", "")}"'
    if el.get("placeholder"):
        line += f' placeholder="{el["placeholder"]}"'
    if el.get("password"):
        line += " password"
    if "value" in el and el["value"] is not None:
        line += f' value="{el["value"]}"'
    if "checked" in el:
        line += " checked" if el["checked"] else " unchecked"
    if "expanded" in el:
        line += " expanded" if el["expanded"] else " collapsed"
    if el.get("disabled"):
        line += " disabled"
    if el.get("href"):
        line += " -> " + _short_href(el["href"], page_url)
    return line


def format_read(page: dict) -> str:
    url = page.get("url", "")
    lines = [f"url: {url}", f"title: {page.get('title', '')}"]
    if page.get("hasPassword"):
        lines.append("page has a password field")
    text = page.get("text") or ""
    total = page.get("textLength") or len(text)
    if text:
        more = f", first {len(text)}" if total > len(text) else ""
        lines.append(f"text ({total} chars{more}):")
        lines.append(text)
    els = page.get("elements") or []
    shown = f"{len(els)} of {page.get('total', len(els))}"
    lines.append(f"elements ({shown}; use element=N):")
    lines.extend(format_element(el, url) for el in els)
    return "\n".join(lines)


def _spec(element, target, selector, role, name, nth) -> dict:
    spec: dict[str, Any] = {}
    if element is not None:
        spec["element"] = int(element)
    if target:
        spec["target"] = target
    if selector:
        spec["selector"] = selector
    if role:
        spec["role"] = role
    if name:
        spec["name"] = name
    if nth and int(nth) > 1:
        spec["nth"] = int(nth)
    return spec


def _texts(text: str | list[str] | None) -> list[str]:
    if text is None:
        return []
    if isinstance(text, list):
        return [str(t) for t in text]
    return [text]


def run(svc, mode: str, p: dict) -> tuple[dict, list]:
    """Execute one Browser call on *svc*. Returns (payload, extra content blocks)."""
    from windows_mcp.browser.service import SEARCH_ENGINES, BrowserError, search_engine, split_keys

    wait_until = p["wait_until"]
    timeout = p["timeout"]
    out: dict[str, Any] = {}
    extra: list = []
    nav = False
    svc.page()  # Attach (or launch) once; a no-op on a warm connection.

    if mode == "open":
        if not p["url"]:
            raise BrowserError("open needs url")
        out.update(svc.goto(p["url"], wait_until=wait_until, timeout=timeout, new_tab=p["new_tab"]))
        nav = True
    elif mode == "search":
        if not p["query"]:
            raise BrowserError("search needs query")
        engine = (p["engine"] or search_engine()).lower()
        if engine not in SEARCH_ENGINES:
            raise BrowserError(f"engine must be one of {', '.join(SEARCH_ENGINES)}")
        url = SEARCH_ENGINES[engine].format(q=urllib.parse.quote_plus(p["query"]))
        out.update(svc.goto(url, wait_until=wait_until, timeout=timeout, new_tab=p["new_tab"]))
        res = svc.search_results(engine, max_results=p["max_results"])
        out["results"] = [
            {k: r[k] for k in ("n", "title", "href", "site", "snippet") if r.get(k)}
            for r in res.get("results") or []
        ]
        if not out["results"]:
            out["note"] = "no results parsed; use read"
        nav = True
    elif mode == "read":
        page = svc.read(max_chars=p["max_chars"], max_elements=p["max_elements"])
        guard = guard_page(svc, page.get("url"), page.get("title"), bool(page.get("hasPassword")))
        text = format_read(page)
        if guard:
            text = f"guard: {json.dumps(guard, ensure_ascii=False)}\n" + text
        return {"_text": text}, []
    elif mode == "click":
        spec = p["spec"]
        if not spec:
            raise BrowserError("click needs element, target, selector or role/name")
        el = svc.resolve(spec, "click")
        guard_input(
            svc, el.get("url"), el.get("title"), bool(el.get("hasPassword")), p["confirmed"]
        )
        since = svc.conn.mark()
        out["clicked"] = format_element(el.get("desc") or {}, el.get("url", ""))
        out["via"] = svc.click(el, clicks=p["clicks"], button=p["button"])
        out.update(svc.settle(since, wait_until=wait_until, timeout=timeout))
        nav = bool(out.get("navigated"))
    elif mode in ("fill", "type"):
        texts = _texts(p["text"])
        if mode == "fill" and not texts:
            raise BrowserError("fill needs text (a string or a list of strings)")
        spec = p["spec"]
        clear = p["clear"] if p["clear"] is not None else mode == "fill"
        if not spec and mode == "fill":
            raise BrowserError("fill needs element, target, selector or role/name")
        if spec:
            el = svc.resolve(spec, "fill")
            password = bool(el.get("password") or el.get("hasPassword"))
            guard_input(svc, el.get("url"), el.get("title"), password, p["confirmed"])
            n = el["n"]
            out["field"] = format_element(el.get("desc") or {}, el.get("url", ""))
        else:
            st = svc.state()
            guard_input(
                svc, st.get("url"), st.get("title"), bool(st.get("hasPassword")), p["confirmed"]
            )
            el, n = None, None
        since = svc.conn.mark()
        for i, item in enumerate(texts or [""]):
            if el is not None and el.get("select"):
                res = svc.js("select", {"n": n, "value": item}) or {}
                if res.get("error"):
                    raise BrowserError(f"no option {item!r}; options: {res.get('options')}")
                continue
            if el is not None and el.get("check"):
                svc.click(el)
                continue
            if n is not None:
                res = svc.js("focus", {"n": n, "clear": clear}) or {}
                if res.get("error"):
                    raise BrowserError("the field is gone from the page; call read again")
                if not res.get("focused") and el.get("hit"):
                    svc.mouse_click(el["x"], el["y"])
                    svc.js("focus", {"n": n, "clear": clear})
            elif clear:
                svc.key("ctrl+a")
            if item:
                svc.insert_text(item)
            elif clear:
                svc.key("Delete")
            if p["submit"]:
                if i == len(texts) - 1:
                    since = svc.conn.mark()  # Only the last Enter is checked for navigation.
                svc.key("Enter")
        if n is not None and not p["submit"]:
            out["value"] = (svc.js("value", {"n": n}) or {}).get("value")
        if len(texts) > 1:
            out["count"] = len(texts)
        if p["submit"]:
            out["submitted"] = True
            out.update(svc.settle(since, wait_until=wait_until, timeout=timeout))
            nav = bool(out.get("navigated"))
    elif mode == "press":
        keys = split_keys(p["keys"] or "")
        if not keys:
            raise BrowserError("press needs keys, e.g. 'Enter' or 'ctrl+a' or 'Tab Tab Enter'")
        if _risky_keys(keys):
            st = svc.state()
            guard_input(
                svc, st.get("url"), st.get("title"), bool(st.get("hasPassword")), p["confirmed"]
            )
        since = svc.conn.mark()
        for k in keys:
            svc.key(k)
        out["pressed"] = keys
        out.update(svc.settle(since, wait_until=wait_until, timeout=timeout))
        nav = bool(out.get("navigated"))
    elif mode == "eval":
        script = p["script"]
        if not script:
            raise BrowserError("eval needs script")
        st = svc.state()
        guard_input(
            svc, st.get("url"), st.get("title"), bool(st.get("hasPassword")), p["confirmed"]
        )
        try:
            value = svc.evaluate(script, timeout=timeout)
        except BrowserError as exc:
            msg = str(exc)
            if "Illegal return" not in msg and "await is only valid" not in msg:
                raise
            value = svc.evaluate(f"(async () => {{\n{script}\n}})()", timeout=timeout)
        dumped = json.dumps(value, ensure_ascii=False, default=str)
        if len(dumped) > p["max_chars"]:
            dumped = dumped[: p["max_chars"]] + "…(truncated)"
        out["value"] = json.loads(dumped) if not dumped.endswith("(truncated)") else dumped
    elif mode == "wait":
        cond = {
            k: v
            for k, v in (
                ("text", p["text"] if isinstance(p["text"], str) else None),
                ("selector", p["selector"]),
                ("url", p["url"]),
            )
            if v
        }
        if p["wait_until"] == "load":
            cond["load"] = True
        if not cond:
            raise BrowserError("wait needs text, selector, url or wait_until='load'")
        if p["gone"]:
            cond["gone"] = True
        res = svc.wait(cond, timeout)
        out["ok"] = bool(res.get("ok"))
        if not out["ok"]:
            out["timeout"] = timeout
    elif mode == "screenshot":
        data = svc.screenshot(full_page=p["full_page"])
        extra.append(Image(data=data, format="jpeg"))
    elif mode in ("back", "forward"):
        out.update(svc.history(-1 if mode == "back" else 1, wait_until=wait_until, timeout=timeout))
        nav = True
    elif mode == "reload":
        out.update(svc.reload(wait_until=wait_until, timeout=timeout))
        nav = True
    elif mode == "tabs":
        action = p["action"] or "list"
        if action == "list":
            svc.page()
            return {"tabs": [{k: v for k, v in t.items() if k != "id"} for t in svc.tabs()]}, []
        if action == "switch":
            if p["tab"] is None:
                raise BrowserError("tabs switch needs tab (number from the list or text)")
            svc.attach(svc.find_tab(p["tab"])["id"], activate=True)
        elif action == "new":
            out.update(svc.new_tab(p["url"], wait_until=wait_until, timeout=timeout))
            nav = bool(p["url"])
        elif action == "close":
            svc.close_tab(p["tab"])
        else:
            raise BrowserError("tabs action must be list, switch, new or close")
    else:
        raise BrowserError(f"unknown mode {mode!r}")

    st = svc.state()
    out = {"url": st.get("url"), "title": st.get("title"), **out}
    if nav:
        guard = guard_page(svc, st.get("url"), st.get("title"), bool(st.get("hasPassword")))
        if guard:
            out["guard"] = guard
    dialogs = svc.take_dialogs()
    if dialogs:
        out["dialogs"] = dialogs
    if p["read"] and mode != "screenshot":
        page = svc.read(max_chars=p["max_chars"], max_elements=p["max_elements"])
        out["_read"] = format_read(page)
    return out, extra


def _call(mode: str, p: dict) -> list | str:
    from windows_mcp.browser.cdp import CDPDisconnected, CDPError
    from windows_mcp.browser.launcher import LaunchError
    from windows_mcp.browser.service import BrowserError, get_service

    svc = get_service()
    start = time.perf_counter()
    if not svc.lock.acquire(timeout=p["timeout"] + 30):
        raise ToolError("BROWSER_BUSY: another Browser call is still running")
    try:
        for attempt in (0, 1):
            try:
                payload, extra = run(svc, mode, p)
                break
            except CDPDisconnected:
                svc.reset()  # Browser was closed: attach/launch again once.
                if attempt:
                    raise ToolError("the browser connection was lost twice; retry") from None
            except (BrowserError, LaunchError) as exc:
                raise ToolError(str(exc)) from None
            except CDPError as exc:
                raise ToolError(f"browser protocol error: {exc}") from None
    finally:
        svc.lock.release()
    ms = round((time.perf_counter() - start) * 1000, 1)
    if "_text" in payload:
        return payload["_text"] + f"\n({ms} ms)"
    read = payload.pop("_read", None)
    payload["ms"] = ms
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if read:
        text += "\n" + read
    return [*extra, text] if extra else text


def _int(value: Any, name: str, default: int, lo: int, hi: int) -> int:
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except TypeError, ValueError:
        raise ToolError(f"{name} must be an integer") from None
    return max(lo, min(hi, n))


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Browser",
        description=DESCRIPTION,
        annotations=ToolAnnotations(
            title="Browser",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @with_analytics(get_analytics(), "Browser-Tool")
    def browser_tool(
        mode: Literal[MODES] = "read",
        url: str | None = None,
        query: str | None = None,
        engine: Literal["bing", "google", "duckduckgo"] | None = None,
        element: int | None = None,
        target: str | None = None,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
        nth: int | None = None,
        text: str | list[str] | None = None,
        keys: str | list[str] | None = None,
        script: str | None = None,
        action: Literal["list", "switch", "new", "close"] | None = None,
        tab: int | str | None = None,
        submit: bool | str = False,
        clear: bool | str | None = None,
        new_tab: bool | str = False,
        read: bool | str = False,
        gone: bool | str = False,
        full_page: bool | str = False,
        clicks: int | None = None,
        button: Literal["left", "right", "middle"] = "left",
        wait_until: Literal["domcontentloaded", "load", "none"] = "domcontentloaded",
        timeout: float | None = None,
        max_chars: int | None = None,
        max_elements: int | None = None,
        max_results: int | None = None,
        confirmed: bool | str = False,
        ctx: Context = None,
    ):
        if mode not in MODES:
            raise ToolError(f"mode must be one of {', '.join(MODES)}")
        try:
            t = float(timeout) if timeout not in (None, "") else 15.0
        except TypeError, ValueError:
            raise ToolError("timeout must be a number of seconds") from None
        params = {
            "url": url,
            "query": query,
            "engine": engine,
            "spec": _spec(element, target, selector, role, name, nth),
            "selector": selector,
            "text": text,
            "keys": keys,
            "script": script,
            "action": action,
            "tab": tab,
            "submit": _as_bool(submit, "submit"),
            "clear": None if clear is None else _as_bool(clear, "clear"),
            "new_tab": _as_bool(new_tab, "new_tab"),
            "read": _as_bool(read, "read"),
            "gone": _as_bool(gone, "gone"),
            "full_page": _as_bool(full_page, "full_page"),
            "clicks": _int(clicks, "clicks", 1, 1, 3),
            "button": button,
            "wait_until": wait_until,
            "timeout": max(0.1, min(120.0, t)),
            "max_chars": _int(max_chars, "max_chars", 3000, 0, 50000),
            "max_elements": _int(max_elements, "max_elements", 60, 0, 500),
            "max_results": _int(max_results, "max_results", 8, 1, 20),
            "confirmed": _as_bool(confirmed, "confirmed"),
        }
        return _call(mode, params)
