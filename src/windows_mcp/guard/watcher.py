"""Background watcher: notice foreground / focus / URL changes and re-run the guard.

Own STA thread with its own ``IUIAutomation`` client (never the shared singleton), like the
WatchDog. Polling only, no UIA event subscriptions (see #332): every loop costs one Win32
foreground/title read; UIA is touched when something changed, for the focus poll and for a
slow URL backstop (single-page apps can navigate without a title change).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

POLL_SECONDS = 0.1
FOCUS_POLL_SECONDS = 0.3
URL_BACKSTOP_SECONDS = 1.5
DEBOUNCE_SECONDS = 0.08
MAX_BACKOFF_SECONDS = 30.0


class GuardWatcher:
    """Polls the desktop and feeds :class:`GuardService.evaluate`."""

    def __init__(
        self,
        service: Any,
        *,
        make_client: Callable[[], tuple[Any, Any]] | None = None,
        foreground: Callable[[], tuple[int, str, int]] | None = None,
    ) -> None:
        self.service = service
        self._make_client = make_client
        self._foreground = foreground
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.probes = 0
        self.last_probe_ms: float | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="wmcp-guard-watcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def poke(self) -> None:
        """Ask for an immediate re-probe (e.g. the gate saw a foreground change)."""
        self._wake.set()

    # ------------------------------------------------------------------ thread
    def _default_client(self) -> tuple[Any, Any]:
        import comtypes.client

        from windows_mcp.uia.core import (
            DEFAULT_UIA_CONNECTION_TIMEOUT_MS,
            DEFAULT_UIA_TRANSACTION_TIMEOUT_MS,
            _AutomationClient,
            _uia_timeout_ms,
        )

        core = _AutomationClient.instance().UIAutomationCore  # interface definitions only
        try:
            client = comtypes.client.CreateObject(
                core.CUIAutomation8, interface=core.IUIAutomation2
            )
            client.ConnectionTimeout = _uia_timeout_ms(
                "WINDOWS_MCP_UIA_CONNECTION_TIMEOUT_MS", DEFAULT_UIA_CONNECTION_TIMEOUT_MS
            )
            client.TransactionTimeout = _uia_timeout_ms(
                "WINDOWS_MCP_UIA_TRANSACTION_TIMEOUT_MS", DEFAULT_UIA_TRANSACTION_TIMEOUT_MS
            )
        except Exception:
            client = comtypes.client.CreateObject(
                "{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=core.IUIAutomation
            )
        return client, core

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            initialized = False
            try:
                if self._make_client is None:
                    import comtypes

                    comtypes.CoInitialize()
                    initialized = True
                client, core = (self._make_client or self._default_client)()
                from windows_mcp.guard.context import Prober, foreground

                prober = Prober(client, core)
                self._loop(prober, self._foreground or foreground)
                backoff = 1.0
            except Exception as exc:
                logger.debug("guard watcher degraded, rebuilding: %s", exc)
            finally:
                if initialized:
                    try:
                        import comtypes

                        comtypes.CoUninitialize()
                    except Exception:
                        pass
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def _loop(self, prober: Any, foreground: Callable[[], tuple[int, str, int]]) -> None:
        last_fg: tuple[int, str] | None = None
        last_focus: tuple | None = None
        last_url: str | None = None
        due: float | None = 0.0  # Probe once at start.
        next_focus = 0.0
        next_url = 0.0
        failures = 0
        while not self._stop.is_set():
            now = time.monotonic()
            hwnd, title, pid = foreground()
            if (hwnd, title) != last_fg:
                last_fg = (hwnd, title)
                due = now + DEBOUNCE_SECONDS
            if self._wake.is_set():
                self._wake.clear()
                due = now
            if hwnd and now >= next_focus:
                next_focus = now + FOCUS_POLL_SECONDS
                try:
                    el = prober.client.GetFocusedElement()
                    rid = tuple(el.GetRuntimeId() or ()) if el else ()
                    if rid != last_focus:
                        last_focus = rid
                        if due is None:
                            due = now
                    failures = 0
                except Exception:
                    failures += 1
            if hwnd and now >= next_url and due is None:
                next_url = now + URL_BACKSTOP_SECONDS
                ctx, _, _, _ = self.service.current()
                if ctx and ctx.get("app") in ("browser", "email") and ctx.get("hwnd") == hwnd:
                    try:
                        url = prober.url(hwnd, bool((ctx.get("focused") or {}).get("address_bar")))
                        if url != last_url:
                            due = now
                    except Exception:
                        failures += 1
            if due is not None and now >= due:
                due = None
                try:
                    ctx = prober.probe(fg=(hwnd, title, pid))
                    last_url = ctx.get("url")
                    self.probes += 1
                    self.last_probe_ms = ctx.get("probe_ms")
                    self.service.evaluate(ctx)
                    failures = 0
                except Exception as exc:
                    failures += 1
                    logger.debug("guard probe failed: %s", exc)
            if failures >= 5:
                raise RuntimeError("guard watcher: repeated UIA failures")
            self._wake.wait(POLL_SECONDS)
