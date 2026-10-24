"""scripts/bench_models.py: skips cleanly without keys and scores routers with mocked HTTP."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bench_models.py"


def load():
    spec = importlib.util.spec_from_file_location("bench_models", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Echo:
    """Decisions API stand-in that answers with the label hidden in the utterance table."""

    def __init__(self, labels):
        self.labels = labels
        self.posts = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts += 1
        picked = self.labels.get(json["state"], "clarify")

        class R:
            status_code = 200

            def json(_self):
                return {
                    "model": "microsoft/microsoft-decision-1-20261009",
                    "answers": {
                        "q": {
                            "type": "choice",
                            "choice": picked,
                            "confidence": 0.9,
                            "probabilities": {picked: 0.9, "clarify": 0.1},
                        }
                    },
                    "usage": {"input_tokens": 80, "output_tokens": 1, "cost": 0.000003},
                }

        return R()


def test_utterance_set_is_balanced():
    bench = load()
    labels = [label for _, label in bench.UTTERANCES]
    assert len(labels) == 40
    assert {label: labels.count(label) for label in set(labels)} == {
        "direct_action": 10,
        "needs_plan": 10,
        "answer_only": 10,
        "clarify": 10,
    }


def test_router_bench_skips_without_keys():
    bench = load()
    lines = []
    report = bench.bench_router(bench.DEFAULT_ROUTER_MODELS, env={}, out=lines.append)
    assert report["models"] == {}
    assert report["skipped"] == {
        "openrouter:microsoft/microsoft-decision-1": "openrouter needs OPENROUTER_API_KEY",
        "anthropic:claude-haiku-5-5": "anthropic needs ANTHROPIC_API_KEY",
        "openrouter:stepfun/step-3.7-flash": "openrouter needs OPENROUTER_API_KEY",
    }
    assert all(line.startswith("skip ") for line in lines)


def test_router_bench_with_mocked_decisions_api():
    bench = load()
    labels = dict(bench.UTTERANCES)
    labels["fix it"] = "answer_only"  # One deliberate miss.
    http = Echo(labels)
    report = bench.bench_router(
        ["openrouter:microsoft/microsoft-decision-1"],
        env={"OPENROUTER_API_KEY": "sk-or-test-fake"},
        http=http,
        out=lambda _: None,
    )
    stats = report["models"]["openrouter:microsoft/microsoft-decision-1"]
    assert stats["n"] == 40 and stats["accuracy"] == 0.975 and stats["errors"] == 0
    assert stats["misses"][0]["text"] == "fix it"
    assert stats["cost_usd"] == round(40 * 0.000003, 6)
    assert http.posts == 41  # 40 + one warm-up.
    json.dumps(report)


def test_cost_from_token_prices():
    bench = load()
    usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 1_000_000}
    assert bench._cost("anthropic:claude-haiku-5-5", usage) == 0.11
    assert bench._cost("openrouter:x", {"cost_usd": 0.5}) == 0.5
