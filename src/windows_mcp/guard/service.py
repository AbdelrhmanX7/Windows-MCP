"""Guard state: evaluate a desktop context, remember it, emit ``security_event``s, gate input.

Pure Python. The UIA probe (``guard/context.py``) and the watcher thread feed contexts in;
the MCP middleware (``tools/guard.py``) reads :meth:`GuardService.current` per call, so the
gate never scans UI Automation on the fast path.

A context is a plain dict::

    {"hwnd": int, "title": str, "process": "msedge.exe", "app": "browser"|"email"|"other",
     "browser": "edge"|..., "url": str|None, "domain": str|None,
     "focused": {"name", "control_type", "is_password"}, "recipients": ["a@b.c", ...],
     "page_text": str|None, "ts": float, "probe_ms": float}
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Callable

from windows_mcp.guard.detector import VERDICT_RANK, Verdict, check, merge_model
from windows_mcp.guard.model import DecisionClient

logger = logging.getLogger(__name__)

_OFF = {"0", "false", "no", "off", "disabled"}

# Tools whose effect lands in the foreground page / message.
TYPING_TOOLS = frozenset({"Type", "MultiEdit"})
# Shortcuts that submit, send or paste into the current page/message.
RISKY_SHORTCUTS = frozenset(
    {
        "enter",
        "return",
        "ctrl+enter",
        "ctrl+return",
        "alt+s",
        "ctrl+v",
        "shift+insert",
        "ctrl+shift+v",
        "ctrl+shift+enter",
    }
)


def guard_enabled() -> bool:
    """``WINDOWS_MCP_GUARD=0`` turns off the watcher, events and the input gate."""
    return os.getenv("WINDOWS_MCP_GUARD", "on").strip().lower() not in _OFF


def events_mode() -> str:
    """``risky`` (default): emit risky verdicts and the return to safe; ``all``: every change."""
    raw = os.getenv("WINDOWS_MCP_GUARD_EVENTS", "risky").strip().lower()
    return "all" if raw == "all" else "risky"


def normalize_shortcut(shortcut: str) -> str:
    keys = [k.strip().lower() for k in re.split(r"\s*\+\s*", shortcut or "") if k.strip()]
    alias = {"control": "ctrl", "ret": "enter", "return": "enter", "ins": "insert"}
    keys = [alias.get(k, k) for k in keys]
    mods = sorted(k for k in keys if k in ("ctrl", "alt", "shift", "win"))
    rest = [k for k in keys if k not in ("ctrl", "alt", "shift", "win")]
    return "+".join(mods + rest)


# Only the browser's own address bar, never page fields like "Email address".
_ADDRESS_TARGET = re.compile(
    r"\baddress (and search )?bar\b|\burl bar\b|\bomnibox\b|\blocation bar\b"
    r"|search or enter address",
    re.I,
)
_PASSWORD_TARGET = re.compile(r"pass(word|code|phrase)|\bpin\b|\botp\b|security code", re.I)
_RISKY_NORMALIZED = frozenset(normalize_shortcut(s) for s in RISKY_SHORTCUTS)


def _as_true(value: Any) -> bool:
    """Only an explicit ``true`` (bool or the string) confirms; anything else does not."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


class GuardService:
    """Holds the latest context + verdict and publishes changes."""

    def __init__(
        self,
        *,
        publish: Callable[[dict], Any] | None = None,
        model: DecisionClient | None = None,
    ) -> None:
        self._publish = publish
        self.model = model if model is not None else DecisionClient()
        self._lock = threading.Lock()
        self._context: dict | None = None
        self._verdict: Verdict | None = None
        self._source: str | None = None
        self._recipient: str | None = None
        self._last_key: tuple | None = None
        self._last_risky = False
        self.events_mode = events_mode()
        self.evaluations = 0

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, ctx: dict, *, emit: bool = True, use_model: bool = True) -> dict:
        """Run the local detector on *ctx*, store it, emit on change, ask the model if gray.

        Returns ``{"verdict": Verdict, "source": ..., "recipient": ...}`` immediately.
        """
        focused = ctx.get("focused") or {}
        password = bool(focused.get("is_password") or ctx.get("has_password_field"))
        verdict: Verdict | None = None
        source = None
        recipient = None
        if ctx.get("url"):
            verdict = check(
                ctx["url"],
                password_field=password,
                text=ctx.get("page_text"),
                title=ctx.get("title"),
            )
            source = "browser"
        for addr in ctx.get("recipients") or []:
            v = check(addr)
            if verdict is None or VERDICT_RANK[v.verdict] > VERDICT_RANK[verdict.verdict]:
                verdict, source, recipient = v, "email", addr
        if verdict is None:
            verdict = Verdict(verdict="unknown", score=0, reasons=["no website or recipient"])
            source = None
        self.evaluations += 1
        with self._lock:
            self._context = dict(ctx)
            self._verdict = verdict
            self._source = source
            self._recipient = recipient
        if emit:
            self._maybe_emit(ctx, verdict, source, recipient)
        if use_model and verdict.gray and verdict.domain and self.model.enabled:
            self._ask_model(ctx, verdict, source, recipient)
        return {"verdict": verdict, "source": source, "recipient": recipient}

    def _ask_model(self, ctx: dict, verdict: Verdict, source: str, recipient: str | None) -> None:
        domain = verdict.domain
        title = ctx.get("title") if source == "browser" else None
        excerpt = ctx.get("page_text") if source == "browser" else None

        def done(result: dict | None) -> None:
            if result is None:
                return
            merged = merge_model(verdict, result)
            with self._lock:
                cur = self._verdict
                if cur is None or cur.domain != domain:
                    return  # The user moved on; the cached result serves the next visit.
                self._verdict = merged
                ctx_now = self._context or ctx
            self._maybe_emit(ctx_now, merged, source, recipient, force=True)

        self.model.submit(domain, done, title=title, excerpt=excerpt, source=source or "browser")

    def _maybe_emit(
        self,
        ctx: dict,
        verdict: Verdict,
        source: str | None,
        recipient: str | None,
        *,
        force: bool = False,
    ) -> None:
        if self._publish is None:
            return
        model_choice = (verdict.model or {}).get("choice")
        key = (source, verdict.domain, recipient, verdict.verdict, model_choice)
        risky = verdict.verdict in ("suspicious", "dangerous")
        with self._lock:
            if key == self._last_key and not force:
                return
            was_risky = self._last_risky
            if self.events_mode == "risky" and not risky and not was_risky:
                self._last_key = key
                return
            if force and key == self._last_key:
                return
            self._last_key = key
            self._last_risky = risky
        self._publish(self.event(ctx, verdict, source, recipient))

    @staticmethod
    def event(ctx: dict, verdict: Verdict, source: str | None, recipient: str | None) -> dict:
        ev: dict[str, Any] = {
            "type": "security_event",
            "event": "verdict",
            "verdict": verdict.verdict,
            "score": verdict.score,
            "domain": verdict.domain,
            "brand": verdict.brand,
            "reasons": list(verdict.reasons),
            "source": source,
            "app": ctx.get("app"),
            "process": ctx.get("process"),
            "title": (ctx.get("title") or "")[:200],
            "password_field": bool(
                (ctx.get("focused") or {}).get("is_password") or ctx.get("has_password_field")
            ),
            "local_ms": round(verdict.ms, 3),
        }
        if source == "browser" and ctx.get("url"):
            ev["url"] = ctx["url"][:500]
        if recipient:
            ev["recipient"] = recipient
        if verdict.model:
            ev["model"] = verdict.model
        return ev

    # ------------------------------------------------------------------ reads
    def current(self) -> tuple[dict | None, Verdict | None, str | None, str | None]:
        with self._lock:
            return self._context, self._verdict, self._source, self._recipient

    def snapshot(self) -> dict:
        ctx, verdict, source, recipient = self.current()
        return {
            "context": ctx,
            "verdict": verdict.to_dict() if verdict else None,
            "source": source,
            "recipient": recipient,
        }

    # ------------------------------------------------------------------ gate
    @staticmethod
    def gated_call(name: str, arguments: dict | None) -> bool:
        """Would this call type into / submit / send the foreground page or message?"""
        args = arguments or {}
        if name == "Type" and _ADDRESS_TARGET.search(str(args.get("target") or "")):
            return False  # Typing a new address is how the user leaves a flagged page.
        if name in TYPING_TOOLS:
            return True
        if name == "Click":
            target = str(args.get("target") or "")
            return bool(target) and not _ADDRESS_TARGET.search(target)
        if name == "Shortcut":
            return normalize_shortcut(str(args.get("shortcut", ""))) in _RISKY_NORMALIZED
        return False

    def risk_for_input(
        self, name: str | None = None, arguments: dict | None = None
    ) -> dict | None:
        """A refusal payload when input into the current context needs user confirmation."""
        ctx, verdict, source, recipient = self.current()
        if verdict is None or ctx is None:
            return None
        if name == "Shortcut" and (ctx.get("focused") or {}).get("address_bar"):
            return None  # Enter / paste in the address bar navigates away; not page input.
        target = str((arguments or {}).get("target") or "")
        password = bool(
            (ctx.get("focused") or {}).get("is_password")
            or ctx.get("has_password_field")
            or _PASSWORD_TARGET.search(target)
        )
        risky = verdict.verdict == "dangerous" or (
            verdict.verdict == "suspicious" and (password or source == "email")
        )
        if not risky:
            return None
        where = f"an email to {recipient}" if source == "email" else f"the page {verdict.domain}"
        brand = f" imitating {verdict.brand}" if verdict.brand else ""
        return {
            "code": "SCAM_GUARD_CONFIRM_REQUIRED",
            "verdict": verdict.verdict,
            "score": verdict.score,
            "domain": verdict.domain,
            "brand": verdict.brand,
            "source": source,
            "recipient": recipient,
            "reasons": list(verdict.reasons)[:6],
            "message": (
                f"Blocked: this would type into or send {where}, which looks "
                f"{verdict.verdict}{brand}. Tell the user what was detected and ask whether to "
                "continue. Only after they explicitly approve, retry the same call with "
                "confirmed=true."
            ),
        }

    def publish_override(self, tool: str, risk: dict) -> None:
        """Record that a risky input went ahead with confirmed=true (audit trail for the UI)."""
        if self._publish is None:
            return
        self._publish(
            {
                "type": "security_event",
                "event": "user_confirmed",
                "tool": tool,
                "verdict": risk.get("verdict"),
                "score": risk.get("score"),
                "domain": risk.get("domain"),
                "brand": risk.get("brand"),
                "source": risk.get("source"),
                "recipient": risk.get("recipient"),
                "reasons": risk.get("reasons", []),
            }
        )

    @staticmethod
    def confirmed(arguments: dict | None) -> bool:
        return _as_true((arguments or {}).get("confirmed"))


_service: GuardService | None = None
_service_lock = threading.Lock()


def get_service() -> GuardService:
    """Process-wide guard service publishing into the task event bus."""
    global _service
    with _service_lock:
        if _service is None:
            from windows_mcp.tasks.runtime import get_runtime

            bus = get_runtime().bus
            _service = GuardService(publish=bus.publish)
        return _service


def set_service(service: GuardService | None) -> None:
    """Replace the singleton (tests)."""
    global _service
    with _service_lock:
        _service = service
