"""Benchmark the in-server fast models.

Two parts:

  router  Latency, accuracy and cost of routing 40 labelled utterances
          (direct_action | needs_plan | answer_only | clarify) per model. Runs anywhere
          with Python + requests (no Windows needed):
            python scripts/bench_models.py router
            python scripts/bench_models.py router --models openrouter:microsoft/microsoft-decision-1,anthropic:claude-haiku-5-5 --reps 2

  do      Do (inner fast model, one MCP call per task) vs an outer model driving the same
          tools one MCP round trip per step, on 5 desktop tasks. Run it in the interactive
          Windows session with the server's dependencies (uv run) and internet access:
            uv run python scripts/bench_models.py do
            uv run python scripts/bench_models.py do --outer-model anthropic:claude-sonnet-5-5

Keys come from ANTHROPIC_API_KEY / OPENROUTER_API_KEY. A model whose key is missing is
skipped with a note saying what it needs; nothing is ever printed about the keys
themselves. Results print as a table; --json PATH also writes them as JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from windows_mcp.models import config  # noqa: E402
from windows_mcp.models.config import ModelError, ModelUnavailable, parse_spec  # noqa: E402
from windows_mcp.models.router import Router  # noqa: E402

DEFAULT_ROUTER_MODELS = [
    "openrouter:microsoft/microsoft-decision-1",
    "anthropic:claude-haiku-5-5",
    "openrouter:stepfun/step-3.7-flash",
]
# USD per million tokens, for providers whose response carries no cost.
PRICES = {
    "anthropic:claude-haiku-5-5": {
        "in": 0.10,
        "out": 0.50,
        "cache_read": 0.01,
        "cache_write": 0.125,
    },
    "anthropic:claude-sonnet-5-5": {"in": 2.0, "out": 10.0, "cache_read": 0.20, "cache_write": 2.5},
    "anthropic:claude-opus-5-5": {"in": 4.0, "out": 20.0, "cache_read": 0.40, "cache_write": 5.0},
}

UTTERANCES: list[tuple[str, str]] = [
    ("open notepad", "direct_action"),
    ("turn the volume up", "direct_action"),
    ("open youtube.com in edge", "direct_action"),
    ("take a screenshot", "direct_action"),
    ("lock my computer", "direct_action"),
    ("open the calculator", "direct_action"),
    ("turn on dark mode", "direct_action"),
    ("mute the sound", "direct_action"),
    ("open my downloads folder", "direct_action"),
    ("pause the music", "direct_action"),
    ("write a note for groceries and then play my favorite songs on spotify", "needs_plan"),
    ("find the cheapest flight to tokyo next week and add it to my calendar", "needs_plan"),
    ("make a powerpoint with three slides about our q3 results", "needs_plan"),
    ("download the latest invoice from my email and save it to the finance folder", "needs_plan"),
    (
        "compare the price of the new iphone on three websites and tell me the cheapest",
        "needs_plan",
    ),
    ("clean up my desktop by moving all screenshots into a new folder", "needs_plan"),
    ("set up a meeting with sarah tomorrow at 3 and email her the agenda", "needs_plan"),
    ("open my resume, update the job title and export it as a pdf", "needs_plan"),
    ("back up my photos folder to the usb drive and then eject it", "needs_plan"),
    ("check the weather and if it will rain remind me at 8 to take an umbrella", "needs_plan"),
    ("what's the capital of australia", "answer_only"),
    ("how many ounces are in a pound", "answer_only"),
    ("what does RAM stand for", "answer_only"),
    ("tell me a joke", "answer_only"),
    ("who wrote pride and prejudice", "answer_only"),
    ("how do you say thank you in japanese", "answer_only"),
    ("what's the difference between a virus and a bacterium", "answer_only"),
    ("how far away is the moon", "answer_only"),
    ("what year did windows 95 come out", "answer_only"),
    ("explain what an api is in one sentence", "answer_only"),
    ("do the thing", "clarify"),
    ("send it to him", "clarify"),
    ("open that file from before", "clarify"),
    ("fix it", "clarify"),
    ("change the setting", "clarify"),
    ("book it", "clarify"),
    ("delete those", "clarify"),
    ("call her", "clarify"),
    ("you know what to do", "clarify"),
    ("move it over there", "clarify"),
]


def _needs(spec_text: str, env: dict) -> str | None:
    """What a model spec needs that the environment lacks, or None when runnable."""
    try:
        spec = parse_spec(spec_text)
        config.api_key(spec.provider, env)
    except ModelUnavailable as exc:
        return str(exc).replace("no model configured: ", "")
    return None


def _cost(spec: str, usage: dict) -> float:
    if "cost" in usage:
        return float(usage["cost"] or 0)
    if "cost_usd" in usage:
        return float(usage["cost_usd"] or 0)
    price = PRICES.get(spec)
    if not price:
        return 0.0
    return (
        usage.get("input_tokens", 0) * price["in"]
        + usage.get("output_tokens", 0) * price["out"]
        + usage.get("cache_read_tokens", 0) * price["cache_read"]
        + usage.get("cache_write_tokens", 0) * price["cache_write"]
    ) / 1e6


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[index], 1)


def bench_router(
    models: list[str],
    *,
    env: dict | None = None,
    reps: int = 1,
    limit: int | None = None,
    http: Any = None,
    out=print,
) -> dict:
    """Run the router benchmark; returns {"models": {spec: stats}, "skipped": {spec: need}}."""
    env = dict(os.environ) if env is None else env
    items = UTTERANCES[:limit] if limit else UTTERANCES
    report: dict = {"models": {}, "skipped": {}, "utterances": len(items), "reps": reps}
    for spec_text in models:
        need = _needs(spec_text, env)
        if need:
            report["skipped"][spec_text] = need
            out(f"skip {spec_text}: {need}")
            continue
        router = Router(parse_spec(spec_text), env, http=http)
        try:
            router.route("warm up the connection")  # Not counted: TLS + cache warm-up.
        except ModelError as exc:
            out(f"warm-up failed for {spec_text}: {exc}")
        latencies, correct, errors, cost = [], 0, 0, 0.0
        rows = []
        for _ in range(reps):
            for text, label in items:
                start = time.perf_counter()
                try:
                    decision = router.route(text)
                except ModelError as exc:
                    errors += 1
                    rows.append({"text": text, "label": label, "error": str(exc)[:200]})
                    continue
                wall = (time.perf_counter() - start) * 1000
                latencies.append(wall)
                correct += decision.choice == label
                cost += _cost(spec_text, decision.usage)
                rows.append(
                    {
                        "text": text,
                        "label": label,
                        "choice": decision.choice,
                        "p": round(decision.probabilities.get(decision.choice, 0), 3),
                        "ms": round(wall, 1),
                    }
                )
        n = len(items) * reps
        stats = {
            "n": n,
            "accuracy": round(correct / n, 3) if n else None,
            "errors": errors,
            "p50_ms": _pct(latencies, 0.5),
            "p95_ms": _pct(latencies, 0.95),
            "mean_ms": round(statistics.fmean(latencies), 1) if latencies else None,
            "cost_usd": round(cost, 6),
            "cost_per_1k_usd": round(cost / n * 1000, 4) if n else None,
            "misses": [r for r in rows if r.get("choice") != r["label"]][:20],
        }
        report["models"][spec_text] = stats
        out(
            f"{spec_text:48s} acc {stats['accuracy']}  p50 {stats['p50_ms']} ms  "
            f"p95 {stats['p95_ms']} ms  errors {errors}  cost ${stats['cost_usd']:.5f}"
        )
    return report


# ---------------------------------------------------------------------------- Do bench

DO_TASKS = [
    {
        "id": "notepad",
        "goal": "Open Notepad and type a grocery list with milk, eggs, bread and apples, one "
        "per line. Do not save.",
        "check": "[bool](Get-Process notepad -ErrorAction SilentlyContinue)",
        "cleanup": "Get-Process notepad -ErrorAction SilentlyContinue | Stop-Process -Force",
    },
    {
        "id": "calculator",
        "goal": "Open Calculator, compute 12*34 in it, and tell me the result.",
        "answer_contains": "408",
        "cleanup": "Get-Process CalculatorApp,Calculator -ErrorAction SilentlyContinue | "
        "Stop-Process -Force",
    },
    {
        "id": "edge",
        "goal": "Open Microsoft Edge to https://example.com",
        "check": "[bool](Get-Process msedge -ErrorAction SilentlyContinue)",
        "cleanup": "Get-Process msedge -ErrorAction SilentlyContinue | Stop-Process -Force",
    },
    {
        "id": "settings",
        "goal": "Switch Windows apps to dark mode using the Settings app (ms-settings:colors).",
        "check": "(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes"
        "\\Personalize').AppsUseLightTheme -eq 0",
        "cleanup": "Set-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes"
        "\\Personalize' AppsUseLightTheme 1; Get-Process SystemSettings -ErrorAction "
        "SilentlyContinue | Stop-Process -Force",
    },
    {
        "id": "find_file",
        "goal": "Find the file named budget-2026-q3.txt somewhere under my Documents folder "
        "and tell me its full path.",
        "setup": "$d = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'bench\\deep'; "
        "New-Item -ItemType Directory -Force $d | Out-Null; "
        "Set-Content (Join-Path $d 'budget-2026-q3.txt') 'x'",
        "answer_contains": "budget-2026-q3.txt",
        "cleanup": "Remove-Item -Recurse -Force (Join-Path ([Environment]::GetFolderPath("
        "'MyDocuments')) 'bench') -ErrorAction SilentlyContinue",
    },
]


def _text(result: Any) -> str:
    return "\n".join(getattr(b, "text", "") for b in getattr(result, "content", None) or [])


async def _ps(client: Any, command: str) -> str:
    result = await client.call_tool("PowerShell", {"command": command}, raise_on_error=False)
    return _text(result)


async def _check(client: Any, task: dict, answer: str) -> bool:
    ok = True
    if task.get("check"):
        ok = "True" in await _ps(client, task["check"])
    if task.get("answer_contains"):
        ok = ok and task["answer_contains"].casefold() in (answer or "").casefold()
    return ok


async def bench_do(args: argparse.Namespace, env: dict) -> dict:
    """Do vs outer-model-per-step on the desktop tasks, over a real stdio MCP session."""
    report: dict = {"tasks": {}, "skipped": {}}
    do_spec = env.get("WINDOWS_MCP_DO_MODEL") or config.DEFAULT_DO_MODEL
    needs = {"do": _needs(do_spec, env), "outer": _needs(args.outer_model, env)}
    for mode, need in needs.items():
        if need:
            report["skipped"][mode] = need
            print(f"skip {mode} ({do_spec if mode == 'do' else args.outer_model}): {need}")
    if all(needs.values()):
        return report
    # Imported only when something will run, so a keyless host needs no server deps.
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    from windows_mcp.agent.loop import DoRun
    from windows_mcp.models.chat import get_chat_client

    command, *cmd_args = args.server_cmd.split()
    transport = StdioTransport(
        command=command, args=cmd_args, env={**env, "ANONYMIZED_TELEMETRY": "false"}
    )
    tasks = [t for t in DO_TASKS if not args.tasks or t["id"] in args.tasks.split(",")]
    async with Client(transport, timeout=300) as client:
        for task in tasks:
            row: dict = {}
            for mode in ("do", "outer"):
                if needs[mode]:
                    continue
                if task.get("setup"):
                    await _ps(client, task["setup"])
                start = time.perf_counter()
                if mode == "do":
                    result = await client.call_tool(
                        "Do",
                        {"goal": task["goal"], "format": "json", "timeout": 120},
                        raise_on_error=False,
                    )
                    text = _text(result)
                    try:
                        summary = json.loads(text)
                    except ValueError:
                        summary = {"status": "error", "answer": text[:300], "usage": {}}
                    spec_text = summary.get("model", do_spec)
                else:
                    outer = get_chat_client(parse_spec(args.outer_model), env)

                    async def call_tool(name, tool_args):
                        return await client.call_tool(name, tool_args)

                    run = DoRun(
                        task["goal"], client=outer, call_tool=call_tool, max_steps=12, timeout=180
                    )
                    summary = await run.run()
                    spec_text = args.outer_model
                wall = round((time.perf_counter() - start) * 1000, 1)
                ok = await _check(client, task, summary.get("answer", ""))
                row[mode] = {
                    "ok": ok,
                    "status": summary.get("status"),
                    "wall_ms": wall,
                    "model_ms": summary.get("model_ms"),
                    "tool_ms": summary.get("tool_ms"),
                    "overhead_per_step_ms": summary.get("overhead_per_step_ms"),
                    "steps": summary.get("step_count"),
                    "turns": summary.get("turns"),
                    "cost_usd": round(_cost(spec_text, summary.get("usage") or {}), 6),
                    "answer": (summary.get("answer") or "")[:200],
                }
                print(
                    f"{task['id']:11s} {mode:5s} ok={ok} {row[mode]['status']} "
                    f"{wall} ms steps={row[mode]['steps']} cost=${row[mode]['cost_usd']}"
                )
                if task.get("cleanup"):
                    await _ps(client, task["cleanup"])
            report["tasks"][task["id"]] = row
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="part", required=True)
    router = sub.add_parser("router", help="router latency/accuracy/cost")
    router.add_argument("--models", default=",".join(DEFAULT_ROUTER_MODELS))
    router.add_argument("--reps", type=int, default=1)
    router.add_argument("--limit", type=int, default=None)
    router.add_argument("--json", dest="json_path", default=None)
    do = sub.add_parser("do", help="Do vs outer-model-per-step on desktop tasks")
    do.add_argument("--outer-model", default="anthropic:claude-sonnet-5-5")
    do.add_argument("--server-cmd", default="uv run windows-mcp serve")
    do.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    do.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args(argv)
    env = dict(os.environ)
    if args.part == "router":
        report = bench_router(
            [m.strip() for m in args.models.split(",") if m.strip()],
            env=env,
            reps=args.reps,
            limit=args.limit,
        )
    else:
        report = asyncio.run(bench_do(args, env))
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
