"""Adapter for decision-scoring models on OpenRouter's Decisions API (Microsoft-Decision-1).

Self-contained on purpose (only ``requests`` via ``models.http``) so other features that
score text, such as a scam guard, can share it.

Confirmed against the docs and live calls (2026-10-24):

- Not a chat model: ``/api/v1/chat/completions`` rejects it with HTTP 400 ("Use the
  /api/alpha/decisions endpoint"), and it is absent from the default ``/api/v1/models``
  list (it appears with ``?output_modalities=decisions``; modality ``text->decisions``).
- ``POST https://openrouter.ai/api/alpha/decisions`` with ``Authorization: Bearer <key>``
  and body ``{"model", "state", "questions"}``. ``state`` is a string, object or array.
  ``questions`` maps names you choose to typed questions, answered in parallel in one call:
  ``choice`` (``instructions`` + ``criteria`` = {label: description}, exactly one label),
  ``noul`` (yes/no, optional ``criteria`` {"true", "false"}), ``score`` (``criteria`` is an
  ordered list, lowest first). Every type needs ``instructions``.
- Response: ``{"model", "answers": {name: {"type": "choice", "choice", "probabilities":
  {label: p}, "confidence"}}, "usage": {"input_tokens", "output_tokens", "cost"}, "id",
  "provider"}``. Validation errors are HTTP 400 with a zod issue list in ``error.message``.

Sources: https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md,
https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from windows_mcp.models.config import ModelError
from windows_mcp.models.http import post_json

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DECISION_1 = "microsoft/microsoft-decision-1"
MAX_STATE_CHARS = 60000  # 32K-token context; keep well inside it.


def is_decision_model(model: str) -> bool:
    """True for models served by the Decisions API rather than chat completions."""
    return model.split(":", 1)[0].startswith(DECISION_1) or "/decision-" in model


def choice(instructions: str, criteria: dict[str, str]) -> dict:
    """A single-label question: pick exactly one of ``criteria`` (label -> description)."""
    if not criteria:
        raise ValueError("choice criteria must not be empty")
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def noul(instructions: str, criteria: dict[str, str] | None = None) -> dict:
    """A yes/no question. ``criteria`` (optional) must have both ``true`` and ``false``."""
    question: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if criteria is not None:
        if set(criteria) != {"true", "false"}:
            raise ValueError("noul criteria needs exactly the keys 'true' and 'false'")
        question["criteria"] = dict(criteria)
    return question


def score(instructions: str, levels: list[str]) -> dict:
    """An ordinal question; ``levels`` are ordered lowest to highest."""
    if not levels:
        raise ValueError("score needs at least one level")
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


@dataclass
class DecisionResult:
    answers: dict
    latency_ms: float
    model: str
    usage: dict = field(default_factory=dict)
    id: str | None = None
    provider: str | None = None


class DecisionClient:
    """Calls the Decisions API. The key is private and never part of ``repr`` or errors."""

    def __init__(
        self,
        api_key: str,
        model: str = DECISION_1,
        *,
        url: str = DECISIONS_URL,
        http: Any = None,
    ) -> None:
        self._key = api_key
        self.model = model
        self.url = url
        self.http = http

    def __repr__(self) -> str:
        return f"DecisionClient(model={self.model!r})"

    @staticmethod
    def build_body(model: str, state: Any, questions: dict[str, dict]) -> dict:
        if not questions:
            raise ValueError("at least one question is required")
        if isinstance(state, str) and len(state) > MAX_STATE_CHARS:
            state = state[:MAX_STATE_CHARS]
        return {"model": model, "state": state, "questions": questions}

    def decide(
        self, state: Any, questions: dict[str, dict], *, timeout: float = 10.0
    ) -> DecisionResult:
        """Ask every question about *state* in one request."""
        body = self.build_body(self.model, state, questions)
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        data, latency = post_json(
            self.url, body, headers, timeout, provider="openrouter decisions", http=self.http
        )
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ModelError("decisions API returned no answers")
        missing = [name for name in questions if name not in answers]
        if missing:
            raise ModelError(f"decisions API did not answer: {', '.join(missing)}")
        return DecisionResult(
            answers=answers,
            latency_ms=latency,
            model=str(data.get("model") or self.model),
            usage=data.get("usage") or {},
            id=data.get("id"),
            provider=data.get("provider"),
        )
