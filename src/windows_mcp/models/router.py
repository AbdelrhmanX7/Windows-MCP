"""Route an utterance and verify a step: calibrated choices from a decision model or an LLM.

With a decision model (default ``openrouter:microsoft/microsoft-decision-1``) a choice is
one Decisions API call. Any chat model also works: it is asked for the same JSON
(``{"choice", "probabilities"}``), which is what the benchmark compares.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from windows_mcp.models import config
from windows_mcp.models.chat import get_chat_client
from windows_mcp.models.config import ModelError, ModelSpec, ModelUnavailable
from windows_mcp.models.decision import DecisionClient, choice, is_decision_model

ROUTE_INSTRUCTIONS = (
    "A user spoke this request to a Windows voice assistant that can operate the PC. "
    "How should the assistant handle it?"
)
ROUTE_OPTIONS = {
    "direct_action": "One or a few obvious actions on the PC (open an app or URL, press a "
    "key, type text, change one setting); no planning needed.",
    "needs_plan": "Several dependent steps, apps or searches; the assistant must plan.",
    "answer_only": "A question or chat answerable in words without touching the PC.",
    "clarify": "Too ambiguous or incomplete to act on safely; ask the user first.",
}
VERIFY_INSTRUCTIONS = (
    "An assistant just performed a step on a Windows PC. Compare the screen state before "
    "and after with the expected outcome. Did the step achieve it?"
)
VERIFY_OPTIONS = {
    "succeeded": "The after state shows the expected outcome.",
    "failed": "The after state does not show the expected outcome and nothing is pending.",
    "still_loading": "The outcome is not visible yet but something is opening or loading.",
}
_JSON = re.compile(r"\{.*\}", re.S)


@dataclass
class Decision:
    """One calibrated choice."""

    choice: str
    probabilities: dict[str, float]
    confidence: float | None
    latency_ms: float
    model: str
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "choice": self.choice,
            "probabilities": {k: round(v, 4) for k, v in self.probabilities.items()},
            "confidence": None if self.confidence is None else round(self.confidence, 4),
            "latency_ms": self.latency_ms,
            "model": self.model,
            "usage": self.usage,
        }


def _normalize(probs: dict[str, Any], labels: list[str]) -> dict[str, float]:
    clean = {}
    for label in labels:
        try:
            clean[label] = max(0.0, float(probs.get(label, 0.0)))
        except TypeError, ValueError:
            clean[label] = 0.0
    total = sum(clean.values())
    if total <= 0:
        return clean
    return {k: v / total for k, v in clean.items()}


def parse_llm_choice(text: str, labels: list[str]) -> tuple[str, dict[str, float]]:
    """Read ``{"choice", "probabilities"}`` from an LLM reply; tolerant of extra text."""
    match = _JSON.search(text or "")
    data: dict = {}
    if match:
        try:
            data = json.loads(match.group())
        except ValueError:
            data = {}
    picked = data.get("choice") if isinstance(data, dict) else None
    probs = data.get("probabilities") if isinstance(data, dict) else None
    if picked not in labels:
        lowered = (text or "").casefold()
        found = [label for label in labels if label.casefold() in lowered]
        picked = found[0] if found else None
    if picked is None:
        raise ModelError(f"model reply names none of {labels}: {(text or '')[:120]!r}")
    if isinstance(probs, dict):
        norm = _normalize(probs, labels)
        if sum(norm.values()) > 0:
            return picked, norm
    return picked, {label: 1.0 if label == picked else 0.0 for label in labels}


def _llm_system(instructions: str, options: dict[str, str]) -> str:
    lines = "\n".join(f"- {k}: {v}" for k, v in options.items())
    return (
        "You are a fast classifier. "
        + instructions
        + "\nOptions:\n"
        + lines
        + '\nReply with only JSON: {"choice": "<option>", "probabilities": {"<option>": '
        "<0..1>, ...}} covering every option. No other text."
    )


class Router:
    """Choice scoring through the configured router model."""

    def __init__(self, spec: ModelSpec, env: dict | None = None, *, http: Any = None) -> None:
        self.spec = spec
        self.timeout = config.timeout(env)
        if spec.provider == "openrouter" and is_decision_model(spec.model):
            self._decider = DecisionClient(config.api_key("openrouter", env), spec.model, http=http)
            self._chat = None
        else:
            self._decider = None
            self._chat = get_chat_client(spec, env, http=http)

    def __repr__(self) -> str:
        return f"Router({self.spec})"

    @classmethod
    def from_env(cls, env: dict | None = None, *, http: Any = None) -> "Router":
        spec = config.router_model(env)
        if spec is None:
            raise ModelUnavailable("no model configured: WINDOWS_MCP_ROUTER_MODEL is off")
        return cls(spec, env, http=http)

    def choose(
        self,
        state: Any,
        instructions: str,
        options: dict[str, str],
        *,
        timeout: float | None = None,
    ) -> Decision:
        """Pick one of *options* for *state*, with a probability for each."""
        if not options:
            raise ValueError("options must not be empty")
        labels = list(options)
        timeout = timeout or self.timeout
        if self._decider is not None:
            result = self._decider.decide(
                state, {"q": choice(instructions, options)}, timeout=timeout
            )
            answer = result.answers["q"]
            probs = _normalize(answer.get("probabilities") or {}, labels)
            picked = answer.get("choice")
            if picked not in labels:
                picked = max(probs, key=probs.get) if probs else labels[0]
            return Decision(
                picked,
                probs,
                answer.get("confidence"),
                result.latency_ms,
                result.model,
                result.usage,
            )
        text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        reply = self._chat.chat(
            _llm_system(instructions, options),
            [{"role": "user", "content": text}],
            [],
            max_tokens=1024,
            timeout=timeout,
        )
        picked, probs = parse_llm_choice(reply.text, labels)
        return Decision(
            picked, probs, max(probs.values()), reply.latency_ms, str(self.spec), reply.usage
        )

    def route(
        self,
        text: str,
        options: dict[str, str] | None = None,
        *,
        instructions: str | None = None,
        timeout: float | None = None,
    ) -> Decision:
        """Classify a user utterance (default options: direct_action/needs_plan/...)."""
        return self.choose(
            text, instructions or ROUTE_INSTRUCTIONS, options or ROUTE_OPTIONS, timeout=timeout
        )

    def verify(
        self, before: str, after: str, expected: str, *, timeout: float | None = None
    ) -> Decision:
        """succeeded / failed / still_loading for one step, from before/after screen text."""
        state = {"expected": expected, "before": before, "after": after}
        return self.choose(state, VERIFY_INSTRUCTIONS, VERIFY_OPTIONS, timeout=timeout)
