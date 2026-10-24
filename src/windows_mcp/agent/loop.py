"""The ``Do`` inner agent loop: a fast model drives this server's own tools in-process.

Pure Python (no Windows imports): the caller passes ``call_tool`` (the server's own
``mcp.call_tool``, so every inner call goes through the control gate, policy, screen lock
and analytics like a direct request), a chat client, and an async ``emit`` for progress.

Context stays small: the first observation is a compact accessibility snapshot (no
screenshot), tool results are clipped when they are appended, and history is append-only
so the provider's prompt cache keeps hitting from turn to turn.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable

from windows_mcp.agent.tools import DONE, inner_args, menu
from windows_mcp.models.config import ModelError, ModelUnavailable

OBS_CHARS = 6000  # Snapshot text kept per observation.
RESULT_CHARS = 1500  # Any other tool result.
TRANSCRIPT_CHARS = 300  # Per-step result kept in the returned transcript.
VERIFY_RETRY_DELAY = 0.7
# Errors that mean "stop now, the user (or another caller) owns the screen".
ABORT_CODES = (
    "USER_CONTROL",
    "TAKEOVER_PENDING",
    "CONTROL_PREEMPTED",
    "CONTROL_UNAVAILABLE",
    "SCREEN_BUSY",
)

SYSTEM_PROMPT = """You operate a Windows PC for one goal, using the tools provided. Speed matters \
more than anything: every turn you take costs the user about a second.

Rules:
1. Use as few turns as possible. When later actions do not depend on seeing the result of \
earlier ones, emit several tool calls in ONE turn, in order (for example App launch, then \
Type, then Done). They run one after another; if one fails the rest are skipped.
2. Prefer direct routes over clicking: PowerShell Start-Process for URIs and URLs \
('ms-settings:bluetooth', 'ms-settings:display', "Start-Process msedge 'https://example.com'", \
'calculator:'), App(mode=launch) for apps, Shortcut for well-known keys. AppSkill gives \
per-app recipes when an app is unfamiliar.
3. Act on UI elements by name: Click(target='Save button'), Type(target='Text editor', \
text=...). 'File > Save As' clicks a menu path. If a target does not resolve you get the top \
candidates back: pick one of their names or coordinates. Use Snapshot only when you truly \
need to look; never guess coordinates.
4. After launching an app, its window may need a moment: use WaitFor(condition=active_window, \
window_name=...) before typing into it if the next step depends on it.
5. Finish with Done(answer, success, expect). expect describes what should be true if you \
succeeded, in terms visible in an accessibility snapshot (a focused window title, a value in \
a display field, an element name); it is checked automatically, so call Done in the same turn as your last \
actions when confident. If the goal is impossible or unsafe, call Done(success=false) and \
say why in answer.
6. Do only what the goal asks. Never delete files, close unsaved work, send messages, buy \
things or change security settings unless the goal explicitly says so.
7. Keep any text you write before tool calls to a few words."""

Emit = Callable[[dict], Awaitable[None]]
CallTool = Callable[[str, dict], Awaitable[Any]]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [clipped {len(text) - limit} chars]"


def _result_text(result: Any) -> str:
    """Flatten a ToolResult (or a plain string) to text; images are never forwarded."""
    if isinstance(result, str):
        return result
    parts = []
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append(block.text)
        else:
            parts.append(f"[{kind} content omitted]")
    if not parts and getattr(result, "structured_content", None) is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False, default=str))
    return "\n".join(parts)


def _abort_code(text: str) -> str | None:
    for code in ABORT_CODES:
        if code in text:
            return code
    return None


def _add_usage(total: dict, usage: dict) -> None:
    for key, value in (usage or {}).items():
        if isinstance(value, (int, float)):
            total[key] = round(total.get(key, 0) + value, 8)


class DoRun:
    """One Do invocation. ``run()`` returns the transcript dict."""

    def __init__(
        self,
        goal: str,
        *,
        client: Any,
        call_tool: CallTool,
        available: set[str] | None = None,
        router: Any = None,
        emit: Emit | None = None,
        max_steps: int = 8,
        timeout: float = 60.0,
        observe: bool = True,
        context: str | None = None,
        max_tokens: int = 1024,
    ) -> None:
        self.goal = goal
        self.client = client
        self.call_tool = call_tool
        self.tools = menu(available)
        self.tool_names = {t.name for t in self.tools}
        self.router = router
        self.emit_cb = emit
        self.max_steps = max(1, int(max_steps))
        self.timeout = max(1.0, float(timeout))
        self.observe = observe and "Snapshot" in self.tool_names
        self.context = context
        self.max_tokens = max_tokens
        self.run_id = "do-" + uuid.uuid4().hex[:8]
        self.steps: list[dict] = []
        self.turns = 0
        self.model_ms = 0.0
        self.tool_ms = 0.0
        self.verify_ms = 0.0
        self.usage: dict = {}
        self.verifications: list[dict] = []
        self.verified: bool | None = None
        self.last_obs = ""

    # ------------------------------------------------------------------ helpers
    async def emit(self, event: str, **fields: Any) -> None:
        if self.emit_cb is None:
            return
        try:
            await self.emit_cb(
                {"type": "do_event", "run_id": self.run_id, "event": event, **fields}
            )
        except Exception:
            pass  # Progress is best effort; it must never break the run.

    def remaining(self) -> float:
        return self.deadline - time.perf_counter()

    async def _tool(self, name: str, args: dict, *, record: bool = True) -> tuple[bool, str]:
        """Run one inner tool through the server. Returns (ok, text)."""
        start = time.perf_counter()
        try:
            result = await self.call_tool(name, args)
            ok, text = True, _result_text(result)
        except Exception as exc:  # ToolError, ValidationError, NotFoundError, ...
            ok, text = False, str(exc) or type(exc).__name__
        latency = round((time.perf_counter() - start) * 1000, 1)
        self.tool_ms += latency
        if record:
            step = {
                "n": len(self.steps) + 1,
                "tool": name,
                "args": args,
                "latency_ms": latency,
                "ok": ok,
                ("result" if ok else "error"): _clip(text, TRANSCRIPT_CHARS),
            }
            self.steps.append(step)
            await self.emit("step", step=step, max_steps=self.max_steps)
        return ok, text

    async def _snapshot(self) -> str:
        ok, text = await self._tool("Snapshot", inner_args("Snapshot", {}), record=False)
        text = _clip(text, OBS_CHARS) if ok else f"(snapshot failed: {text[:200]})"
        self.last_obs = text
        return text

    async def _verify(self, before: str, expect: str, actions: list[str]) -> dict | None:
        """Ask the router whether Done's ``expect`` holds; None when no verifier is usable.

        The state holds the screen before and after plus what the actions reported, since
        an accessibility snapshot does not always show typed text or values.
        """
        if self.router is None or not expect or not self.observe:
            return None
        for attempt in range(2):
            after = await self._snapshot()
            start = time.perf_counter()
            try:
                decision = await asyncio.to_thread(
                    self.router.verify,
                    before,
                    after,
                    expect,
                    actions=actions,
                    timeout=max(0.5, min(10.0, self.remaining())),
                )
            except (ModelError, ModelUnavailable, ValueError) as exc:
                self.verify_ms += (time.perf_counter() - start) * 1000
                await self.emit("verify", error=str(exc))
                return None
            self.verify_ms += (time.perf_counter() - start) * 1000
            _add_usage(self.usage, {"verify_cost": (decision.usage or {}).get("cost", 0)})
            record = {"attempt": attempt + 1, **decision.to_dict()}
            self.verifications.append(record)
            await self.emit("verify", verification=record)
            if decision.choice != "still_loading" or self.remaining() < VERIFY_RETRY_DELAY + 1:
                return record
            await asyncio.sleep(VERIFY_RETRY_DELAY)
        return record

    # ------------------------------------------------------------------ main loop
    async def run(self) -> dict:
        self.t0 = time.perf_counter()
        self.deadline = self.t0 + self.timeout
        await self.emit(
            "started",
            goal=self.goal,
            max_steps=self.max_steps,
            model=f"{self.client.provider}:{self.client.model}",
        )
        status, answer, success = "error", "", False
        try:
            status, answer, success = await self._loop()
        except asyncio.CancelledError:
            status, answer = "cancelled", "cancelled"
            raise
        finally:
            self.summary = self._summary(status, answer, success)
            await self.emit("finished", **{k: v for k, v in self.summary.items() if k != "steps"})
        return self.summary

    async def _loop(self) -> tuple[str, str, bool]:
        intro = f"Goal: {self.goal}"
        if self.context:
            intro += f"\nContext: {self.context}"
        first_obs = ""
        if self.observe:
            first_obs = await self._snapshot()
            intro += f"\n\nCurrent screen (accessibility snapshot):\n{first_obs}"
        messages: list[dict] = [{"role": "user", "content": intro}]
        before = first_obs
        actions = 0
        rejections = 0
        max_turns = self.max_steps + 2
        while self.turns < max_turns:
            if self.remaining() <= 0.5:
                return "timeout", f"timed out after {self.timeout:g}s", False
            self.turns += 1
            try:
                reply = await asyncio.to_thread(
                    self.client.chat,
                    SYSTEM_PROMPT,
                    messages,
                    self.tools,
                    max_tokens=self.max_tokens,
                    timeout=max(0.5, self.remaining()),
                )
            except (ModelError, ModelUnavailable) as exc:
                return "error", str(exc), False
            self.model_ms += reply.latency_ms
            _add_usage(self.usage, reply.usage)
            messages.append(reply.message)
            await self.emit(
                "model",
                turn=self.turns,
                latency_ms=reply.latency_ms,
                tool_calls=[c.name for c in reply.tool_calls],
                text=_clip(reply.text, 200),
            )
            if not reply.tool_calls:
                # Plain text with no tools: treat as the final answer.
                return "done", reply.text.strip(), True

            results: list[dict] = []
            done_call = None
            skip_reason = None
            for call in reply.tool_calls:
                if done_call is not None or skip_reason is not None:
                    results.append(
                        {
                            "id": call.id,
                            "name": call.name,
                            "is_error": True,
                            "content": skip_reason or "skipped: Done was already called",
                        }
                    )
                    continue
                if call.name == DONE:
                    done_call = call
                    results.append({"id": call.id, "name": call.name, "content": "ok"})
                    continue
                if call.name not in self.tool_names:
                    results.append(
                        {
                            "id": call.id,
                            "name": call.name,
                            "is_error": True,
                            "content": f"unknown tool {call.name!r}",
                        }
                    )
                    skip_reason = f"skipped: earlier call {call.name} failed"
                    continue
                if actions >= self.max_steps:
                    return "max_steps", f"stopped after {self.max_steps} steps", False
                if self.remaining() <= 0.2:
                    return "timeout", f"timed out after {self.timeout:g}s", False
                actions += 1
                args = inner_args(call.name, call.args)
                ok, text = await self._tool(call.name, args)
                code = None if ok else _abort_code(text)
                if code:
                    return "blocked", f"{code}: {_clip(text, 300)}", False
                limit = OBS_CHARS if call.name == "Snapshot" else RESULT_CHARS
                if call.name == "Snapshot" and ok:
                    self.last_obs = _clip(text, OBS_CHARS)
                results.append(
                    {
                        "id": call.id,
                        "name": call.name,
                        "content": _clip(text, limit),
                        **({} if ok else {"is_error": True}),
                    }
                )
                if not ok:
                    skip_reason = f"skipped: earlier call {call.name} failed"

            if done_call is not None and skip_reason is None:
                args = done_call.args
                answer = str(args.get("answer") or "").strip()
                success = bool(args.get("success", True))
                expect = str(args.get("expect") or "").strip()
                if not success:
                    return "failed", answer, False
                evidence = [
                    f"{st['tool']} {json.dumps(st['args'], ensure_ascii=False)[:200]} -> "
                    f"{'ok' if st['ok'] else 'failed'}: {st.get('result') or st.get('error')}"
                    for st in self.steps[-6:]
                ]
                check = await self._verify(before, expect, evidence)
                if check is None:
                    return "done", answer, True
                if check["choice"] != "failed":
                    self.verified = True
                    return "done", answer, True
                rejections += 1
                if rejections > 1:
                    # Second disagreement: report the model's answer, flagged unverified.
                    self.verified = False
                    return "done", answer, True
                # The verifier disagrees: let the model see the screen and try once more.
                for r in results:
                    if r["id"] == done_call.id:
                        r["is_error"] = True
                        r["content"] = (
                            "Not confirmed: the screen does not seem to show the expected "
                            f"outcome ({expect}). Fix it if something is wrong, otherwise call "
                            f"Done again. Screen now:\n{self.last_obs}"
                        )
            messages.append({"role": "tool_results", "results": results})
        return "max_steps", f"stopped after {self.turns} model turns", False

    def _summary(self, status: str, answer: str, success: bool) -> dict:
        total = round((time.perf_counter() - self.t0) * 1000, 1)
        model_ms = round(self.model_ms, 1)
        tool_ms = round(self.tool_ms, 1)
        verify_ms = round(self.verify_ms, 1)
        overhead = round(max(0.0, total - model_ms - tool_ms - verify_ms), 1)
        n = len(self.steps)
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": status,
            "ok": status == "done" and success,
            "answer": answer,
            "model": f"{self.client.provider}:{self.client.model}",
            "turns": self.turns,
            "step_count": n,
            "total_ms": total,
            "model_ms": model_ms,
            "tool_ms": tool_ms,
            "verify_ms": verify_ms,
            "overhead_ms": overhead,
            "overhead_per_step_ms": round(overhead / n, 1) if n else None,
            "usage": self.usage,
            "verified": self.verified,
            "verifications": self.verifications,
            "steps": self.steps,
        }


def format_text(summary: dict) -> str:
    """Short human/planner-readable transcript."""
    head = (
        f"Do {summary['status']} ({'ok' if summary['ok'] else 'not ok'}): {summary['answer']}\n"
        f"{summary['step_count']} steps, {summary['turns']} model turns, total "
        f"{summary['total_ms']} ms (model {summary['model_ms']} ms, tools "
        f"{summary['tool_ms']} ms, verify {summary['verify_ms']} ms, overhead "
        f"{summary['overhead_ms']} ms) via {summary['model']}"
    )
    lines = [head]
    for step in summary["steps"]:
        args = json.dumps(step["args"], ensure_ascii=False)
        if len(args) > 120:
            args = args[:120] + "..."
        body = step.get("result") if step["ok"] else step.get("error")
        first = (body or "").strip().splitlines()[0] if body else ""
        lines.append(
            f"[{step['n']}] {step['tool']} {args} {step['latency_ms']} ms "
            f"{'ok' if step['ok'] else 'FAILED'}" + (f": {first[:160]}" if first else "")
        )
    for v in summary.get("verifications") or []:
        lines.append(f"verify #{v['attempt']}: {v['choice']} ({v['latency_ms']} ms)")
    if summary.get("verified") is False:
        lines.append("NOTE: the final screen check did not confirm the result.")
    return "\n".join(lines)
