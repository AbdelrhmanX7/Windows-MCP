"""Do and Route tools: fast models inside the server, so one planner turn does many steps."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from windows_mcp.agent.loop import DoRun, format_text
from windows_mcp.infrastructure import with_analytics
from windows_mcp.models import config
from windows_mcp.models.chat import get_chat_client
from windows_mcp.models.config import ModelError, ModelUnavailable
from windows_mcp.models.router import ROUTE_OPTIONS, Router
from windows_mcp.tasks.runtime import TaskRuntime, get_runtime
from windows_mcp.tasks.store import _as_bool

logger = logging.getLogger(__name__)

MAX_STEPS_LIMIT = 30
MAX_TIMEOUT = 300.0

DO_DESCRIPTION = (
    "Hand a small, concrete desktop goal to a fast model that runs inside this server and "
    "drives the tools itself (Snapshot, Click/Type by target name, Shortcut, App, Scroll, "
    "Wait/WaitFor, PowerShell, AppSkill/Apps, Locate), so one call does many steps, e.g. "
    "'open Notepad and type a grocery list: milk, eggs', 'open Calculator and compute 12*34', "
    "'turn on dark mode in Settings'. Returns a short transcript: status, answer, every step "
    "with tool, latency_ms and ok, and total ms split into model, tool, verify and overhead. "
    "max_steps caps tool calls (default 8); timeout is seconds for the whole run (default "
    "60). context adds facts the inner model needs (file names, the user's preferences). "
    "verify=true (default) lets a decision model check the final screen instead of an extra "
    "model turn. Every inner call passes the same control gate, policy and screen lock as a "
    "direct call; Do holds the screen for its whole run. Progress streams as do_event task "
    "events and MCP progress. Fails with 'no model configured' when WINDOWS_MCP_DO_MODEL's "
    "provider key is not set. Use individual tools instead for one-step actions or when "
    "you need to see the screen yourself."
)

ROUTE_DESCRIPTION = (
    "Score text against a fixed set of options with a fast decision model (default "
    "Microsoft-Decision-1 via OpenRouter, ~0.3-0.6 s) and return the choice plus a calibrated "
    "probability per option. mode='route' (default): text is a user utterance and options "
    "default to direct_action | needs_plan | answer_only | clarify; pass options as a list "
    "of labels or a {label: description} object, and instructions to change the question. "
    "mode='verify': did a step work? before/after are screen texts (e.g. Snapshot output) "
    "and expected the intended outcome; returns succeeded | failed | still_loading. "
    "format='json' for machine output. Fails with 'no model configured' when the router "
    "model's key is not set."
)


def _options(raw: Any) -> dict[str, str] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [p.strip() for p in raw.replace("|", ",").split(",") if p.strip()]
    if isinstance(raw, list):
        if not raw or not all(isinstance(x, str) and x for x in raw):
            raise ValueError("options must be a non-empty list of labels")
        return {label: ROUTE_OPTIONS.get(label, label.replace("_", " ")) for label in raw}
    if isinstance(raw, dict):
        if not raw or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
            raise ValueError("options must map labels to string descriptions")
        return dict(raw)
    raise ValueError("options must be a list of labels or a {label: description} object")


async def _registered_tools(mcp: Any) -> set[str] | None:
    """Names the server really exposes (honors --tools/--exclude-tools); None if unknown."""
    for attr in ("list_tools", "get_tools"):
        fn = getattr(mcp, attr, None)
        if fn is None:
            continue
        try:
            tools = await fn()
        except TypeError:
            continue
        except Exception:
            logger.debug("Could not list tools for Do", exc_info=True)
            return None
        if isinstance(tools, dict):
            return set(tools)
        return {getattr(t, "name", None) for t in tools} - {None}
    return None


def register(mcp, *, get_desktop, get_analytics, runtime: TaskRuntime | None = None):
    """Register Do and Route."""

    def rt() -> TaskRuntime:
        return runtime or get_runtime()

    @mcp.tool(
        name="Do",
        description=DO_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Do",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @with_analytics(get_analytics(), "Do-Tool")
    async def do_tool(
        goal: str,
        max_steps: int = 8,
        timeout: float = 60.0,
        context: str | None = None,
        verify: bool | str = True,
        format: Literal["text", "json"] = "text",
        ctx: Context = None,
    ) -> str:
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal must be a non-empty string")
        if (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or not (1 <= max_steps <= MAX_STEPS_LIMIT)
        ):
            raise ValueError(f"max_steps must be an integer between 1 and {MAX_STEPS_LIMIT}")
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= MAX_TIMEOUT:
            raise ValueError(f"timeout must be between 1 and {MAX_TIMEOUT:g} seconds")
        try:
            client = get_chat_client(config.do_model())
        except ModelUnavailable as exc:
            raise ToolError(f"Do unavailable: {exc}") from None
        router = None
        if _as_bool(verify, "verify"):
            try:
                router = Router.from_env()
            except ModelUnavailable:
                router = None  # Done is trusted without a verifier.
        bus = rt().bus

        async def emit(event: dict) -> None:
            bus.publish(event)
            if ctx is not None and event.get("event") == "step":
                step = event["step"]
                try:
                    await ctx.report_progress(
                        progress=step["n"],
                        total=max_steps,
                        message=f"{step['tool']} {'ok' if step['ok'] else 'failed'} "
                        f"({step['latency_ms']} ms)",
                    )
                except Exception:
                    pass

        run = DoRun(
            goal.strip(),
            client=client,
            call_tool=lambda name, args: mcp.call_tool(name, args),
            available=await _registered_tools(mcp),
            router=router,
            emit=emit,
            max_steps=max_steps,
            timeout=float(timeout),
            context=context,
        )
        summary = await run.run()
        if format == "json":
            return json.dumps(summary, ensure_ascii=False, default=str)
        return format_text(summary)

    @mcp.tool(
        name="Route",
        description=ROUTE_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Route",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    @with_analytics(get_analytics(), "Route-Tool")
    async def route_tool(
        text: str | None = None,
        options: list[str] | dict[str, str] | str | None = None,
        instructions: str | None = None,
        mode: Literal["route", "verify"] = "route",
        before: str | None = None,
        after: str | None = None,
        expected: str | None = None,
        format: Literal["text", "json"] = "text",
        ctx: Context = None,
    ) -> str:
        try:
            router = Router.from_env()
        except ModelUnavailable as exc:
            raise ToolError(f"Route unavailable: {exc}") from None
        try:
            if mode == "verify":
                if not expected or after is None:
                    raise ValueError("mode='verify' needs expected and after (before optional)")
                decision = await asyncio.to_thread(router.verify, before or "", after, expected)
            else:
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("text must be a non-empty string")
                decision = await asyncio.to_thread(
                    router.route, text, _options(options), instructions=instructions
                )
        except ModelError as exc:
            raise ToolError(f"Route failed: {exc}") from None
        data = decision.to_dict()
        if format == "json":
            return json.dumps(data, ensure_ascii=False)
        probs = ", ".join(
            f"{k}={v:.2f}" for k, v in sorted(data["probabilities"].items(), key=lambda kv: -kv[1])
        )
        conf = "" if data["confidence"] is None else f", confidence {data['confidence']:.2f}"
        return (
            f"{mode}: {data['choice']}{conf} ({data['latency_ms']} ms via {data['model']})\n"
            f"probabilities: {probs}"
        )
