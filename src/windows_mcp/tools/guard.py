"""Context and CheckLink tools, the scam-guard input gate, and the guard watcher lifecycle."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.types import ToolAnnotations

from windows_mcp.desktop.utils import as_bool as _as_bool
from windows_mcp.guard.service import GuardService, get_service, guard_enabled
from windows_mcp.infrastructure import with_analytics

logger = logging.getLogger(__name__)

CACHE_MAX_AGE = 2.0  # Seconds a watcher context may be reused for the same window.
MAX_LINKS = 20

CONTEXT_DESCRIPTION = (
    "Read-only, fast (~ms): where the user is right now. Returns the foreground window "
    "(title, process, app kind), the browser URL and registrable domain (Edge/Chrome/Brave/"
    "Firefox address bar), the focused field (name, control type, is_password), email "
    "recipients (To/Cc) in Outlook or webmail compose, and the scam-guard verdict for that "
    "site/recipient (safe|suspicious|dangerous|unknown, score, brand, reasons). Served from "
    "the background watcher when fresh; refresh=true forces a new read."
)
CHECK_LINK_DESCRIPTION = (
    "Read-only, sub-millisecond local check of a URL, domain or email address (or a list) for "
    "phishing/lookalike signs before opening a link or sending mail, e.g. 'rnicorsoft.com' "
    "imitating microsoft, 'paypa1.com', 'microsoft.com.verify-login.xyz', "
    "'support@rnicorsoft.com', 'https://microsoft.com@evil.xyz'. Returns verdict "
    "(safe|suspicious|dangerous|unknown), score 0-100, brand, reasons and ms per target. "
    "second_opinion=true also waits (bounded) for the optional Microsoft-Decision-1 model on "
    "gray-zone targets when OPENROUTER_API_KEY is set; it can only raise severity."
)

_watcher = None


def _foreground() -> tuple[int, str, int]:
    from windows_mcp.guard.context import foreground

    return foreground()


def _matches(ctx: dict | None, fg: tuple[int, str, int]) -> bool:
    if not ctx or ctx.get("hwnd") != fg[0]:
        return False
    # Browser tabs share one window; the title tells tabs/pages apart.
    return ctx.get("app") not in ("browser",) or ctx.get("title") == fg[1]


def _probe_sync(fg: tuple[int, str, int], text: str = "auto") -> dict:
    from windows_mcp.guard.context import shared_prober

    return shared_prober().probe(text=text, fg=fg)


def _context_payload(ctx: dict, result: dict | Any, *, cached: bool, ms: float) -> dict:
    verdict = result["verdict"] if isinstance(result, dict) else result
    page_text = ctx.get("page_text")
    return {
        "window": {
            "title": ctx.get("title"),
            "process": ctx.get("process"),
            "app": ctx.get("app"),
            "product": ctx.get("product"),
        },
        "url": ctx.get("url"),
        "domain": ctx.get("domain"),
        "focused": ctx.get("focused") or {},
        "recipients": ctx.get("recipients") or [],
        "page_text_chars": len(page_text) if page_text else 0,
        "guard": verdict.to_dict() if verdict else None,
        "cached": cached,
        "probe_ms": ctx.get("probe_ms"),
        "ms": round(ms, 2),
    }


class GuardGate(Middleware):
    """Refuse Type/MultiEdit/Click(target)/send-shortcuts into a risky page or email.

    Reads the cached guard state; only when the foreground window (or browser tab title)
    differs from the cached context does it run one UIA probe (~tens of ms).
    """

    def __init__(
        self,
        service: Callable[[], GuardService] = get_service,
        *,
        foreground: Callable[[], tuple[int, str, int]] = _foreground,
        probe: Callable[[tuple[int, str, int]], dict] | None = None,
    ) -> None:
        self._service = service
        self._foreground = foreground
        self._probe = probe

    async def _refresh(self, service: GuardService) -> None:
        fg = self._foreground()
        if not fg[0]:
            return  # No interactive desktop (session 0): nothing to type into.
        ctx, _, _, _ = service.current()
        fresh = ctx is not None and (
            (_watcher is not None and _watcher.running) or time.time() - ctx.get("ts", 0) < 1.0
        )
        if _matches(ctx, fg) and fresh:
            return
        if self._probe is not None:
            new = self._probe(fg)
        else:
            from windows_mcp.infrastructure.thread_affinity import run_uia_affine

            new = await run_uia_affine(_probe_sync, fg)
        service.evaluate(new)
        if _watcher is not None:
            _watcher.poke()

    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable) -> Any:
        name = context.message.name
        arguments = getattr(context.message, "arguments", None)
        if not GuardService.gated_call(name, arguments):
            return await call_next(context)
        service = self._service()
        try:
            await self._refresh(service)
        except Exception:
            logger.debug("guard refresh failed; using cached state", exc_info=True)
        risk = service.risk_for_input()
        if risk is not None:
            if GuardService.confirmed(arguments):
                service.publish_override(name, risk)
            else:
                raise ToolError(json.dumps(risk, ensure_ascii=False))
        return await call_next(context)


def start_guard() -> Any:
    """Start the watcher (called from the server lifespan). Returns it, or None when off."""
    global _watcher
    if not guard_enabled() or _watcher is not None:
        return _watcher
    try:
        from windows_mcp.guard import warm
        from windows_mcp.guard.watcher import GuardWatcher

        warm()
        service = get_service()
        service.model.warm_connection()
        _watcher = GuardWatcher(service)
        _watcher.start()
    except Exception:
        logger.warning("Scam guard watcher could not start", exc_info=True)
        _watcher = None
    return _watcher


def stop_guard() -> None:
    global _watcher
    watcher, _watcher = _watcher, None
    if watcher is not None:
        watcher.stop()


def _check_links(targets: list[str], second_opinion: bool, service: GuardService) -> list[dict]:
    from windows_mcp.guard import check, merge_model

    out = []
    for target in targets:
        v = check(target)
        entry = v.to_dict()
        entry["target"] = target
        model = service.model
        if v.gray and v.domain and model.enabled:
            hit = model.cached(v.domain, "email" if v.kind == "email" else "browser")
            if hit is None and second_opinion:
                hit = model.classify(v.domain, source="email" if v.kind == "email" else "browser")
            if hit is not None:
                entry = merge_model(v, hit).to_dict()
                entry["target"] = target
            elif not second_opinion:
                model.submit(
                    v.domain,
                    lambda _r: None,
                    source="email" if v.kind == "email" else "browser",
                )
                entry["second_opinion"] = "pending (cached for the next check)"
        out.append(entry)
    return out


def register(mcp, *, get_desktop, get_analytics):
    """Register Context and CheckLink and install the guard gate (inside orchestration)."""
    middleware = getattr(mcp, "middleware", None)
    if isinstance(middleware, list) and guard_enabled():
        # tools/tasks.py later inserts OrchestrationMiddleware at 0, giving
        # Orchestration -> GuardGate -> ControlToolGate: a refusal never takes a lease.
        middleware.insert(0, GuardGate())

    @mcp.tool(
        name="Context",
        description=CONTEXT_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Context",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Context-Tool", run_sync_in_thread=False)
    def context_tool(
        refresh: bool | str = False,
        include_text: bool | str = False,
        ctx: Context = None,
    ) -> str:
        start = time.perf_counter()
        refresh = _as_bool(refresh, "refresh")
        include_text = _as_bool(include_text, "include_text")
        service = get_service()
        fg = _foreground()
        cached_ctx, verdict, source, recipient = service.current()
        if (
            not refresh
            and not include_text
            and _matches(cached_ctx, fg)
            and time.time() - cached_ctx.get("ts", 0) < CACHE_MAX_AGE
            and verdict is not None
        ):
            payload = _context_payload(
                cached_ctx, verdict, cached=True, ms=(time.perf_counter() - start) * 1000
            )
        else:
            new = _probe_sync(fg, text="always" if include_text else "auto")
            result = service.evaluate(new)
            payload = _context_payload(
                new, result, cached=False, ms=(time.perf_counter() - start) * 1000
            )
            source, recipient = result["source"], result["recipient"]
        payload["source"] = source
        if recipient:
            payload["recipient"] = recipient
        return json.dumps(payload, ensure_ascii=False, default=str)

    @mcp.tool(
        name="CheckLink",
        description=CHECK_LINK_DESCRIPTION,
        annotations=ToolAnnotations(
            title="CheckLink",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    @with_analytics(get_analytics(), "CheckLink-Tool")
    def check_link_tool(
        target: str | list[str],
        second_opinion: bool | str = False,
        ctx: Context = None,
    ) -> str:
        second_opinion = _as_bool(second_opinion, "second_opinion")
        targets = [target] if isinstance(target, str) else list(target or [])
        targets = [t for t in targets if isinstance(t, str) and t.strip()]
        if not targets:
            raise ValueError("target must be a URL, domain or email address (or a list of them)")
        if len(targets) > MAX_LINKS:
            raise ValueError(f"at most {MAX_LINKS} targets per call")
        results = _check_links(targets, second_opinion, get_service())
        if len(results) == 1:
            return json.dumps(results[0], ensure_ascii=False)
        return json.dumps({"results": results}, ensure_ascii=False)
