"""Tool-calling chat clients: Anthropic Messages API, OpenRouter (OpenAI-compatible), script.

Plain ``requests`` (as in ``resolver/vision.py``), no SDKs. Conversations use one neutral
message format so the ``Do`` loop does not care which provider runs it:

- ``{"role": "user", "content": str}``
- ``{"role": "assistant", "text": str, "tool_calls": [ToolCall], "raw": ..., "provider": str}``
  (``raw`` is the provider's own content, replayed unchanged to the same provider so
  Anthropic thinking blocks and the prompt cache stay valid; history is append-only)
- ``{"role": "tool_results", "results": [{"id", "name", "content", "is_error"}]}``

Keys are held privately and never appear in ``repr``, logs or error messages.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from windows_mcp.models import config
from windows_mcp.models.config import ModelError, ModelSpec, ModelUnavailable
from windows_mcp.models.http import post_json

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
_CACHE = {"type": "ephemeral"}


@dataclass
class ToolSpec:
    """One tool offered to the model: name, description and JSON-schema parameters."""

    name: str
    description: str
    schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ChatReply:
    """A normalized model turn. ``message`` is what to append to the history."""

    text: str
    tool_calls: list[ToolCall]
    usage: dict
    latency_ms: float
    stop_reason: str | None
    message: dict = field(default_factory=dict)


class ChatClient(Protocol):
    provider: str
    model: str

    def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ChatReply: ...


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return {"_unparsed": raw}
        return value if isinstance(value, dict) else {"_value": value}
    return {}


# ---------------------------------------------------------------------------- Anthropic


class AnthropicChat:
    """Claude via the Messages API with prompt caching on tools+system and the newest turn."""

    provider = "anthropic"

    def __init__(
        self, api_key: str, model: str, *, effort: str | None = "low", http: Any = None
    ) -> None:
        self._key = api_key
        self.model = model
        self.effort = effort
        self.http = http

    def __repr__(self) -> str:
        return f"AnthropicChat(model={self.model!r})"

    @staticmethod
    def _messages(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for msg in messages:
            role = msg["role"]
            if role == "user":
                out.append({"role": "user", "content": [{"type": "text", "text": msg["content"]}]})
            elif role == "assistant":
                if msg.get("provider") == "anthropic" and msg.get("raw"):
                    content = msg["raw"]
                else:
                    content = []
                    if msg.get("text"):
                        content.append({"type": "text", "text": msg["text"]})
                    for call in msg.get("tool_calls") or []:
                        content.append(
                            {
                                "type": "tool_use",
                                "id": call.id,
                                "name": call.name,
                                "input": call.args,
                            }
                        )
                out.append({"role": "assistant", "content": content})
            elif role == "tool_results":
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": r["id"],
                        "content": r["content"],
                        **({"is_error": True} if r.get("is_error") else {}),
                    }
                    for r in msg["results"]
                ]
                out.append({"role": "user", "content": blocks})
        # Rolling cache breakpoint on the newest block (a copy: history itself never changes).
        if out and out[-1]["content"]:
            last = dict(out[-1])
            blocks = list(last["content"])
            blocks[-1] = {**blocks[-1], "cache_control": _CACHE}
            last["content"] = blocks
            out[-1] = last
        return out

    def build_body(
        self, system: str, messages: list[dict], tools: list[ToolSpec], max_tokens: int
    ) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            # Render order is tools -> system -> messages: this breakpoint caches both.
            "system": [{"type": "text", "text": system, "cache_control": _CACHE}],
            "messages": self._messages(messages),
        }
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.schema}
                for t in tools
            ]
            body["tool_choice"] = {"type": "auto"}
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        return body

    def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ChatReply:
        body = self.build_body(system, messages, tools, max_tokens)
        headers = {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        data, latency = post_json(
            ANTHROPIC_URL,
            body,
            headers,
            timeout or config.timeout(),
            provider="anthropic",
            http=self.http,
        )
        content = data.get("content") or []
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = [
            ToolCall(b.get("id", ""), b.get("name", ""), _parse_args(b.get("input")))
            for b in content
            if b.get("type") == "tool_use"
        ]
        usage = data.get("usage") or {}
        norm = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
        }
        message = {
            "role": "assistant",
            "text": text,
            "tool_calls": calls,
            "raw": content,
            "provider": "anthropic",
        }
        return ChatReply(text, calls, norm, latency, data.get("stop_reason"), message)


# ---------------------------------------------------------------------------- OpenRouter


class OpenRouterChat:
    """Any OpenRouter chat model through its OpenAI-compatible endpoint (tool calling)."""

    provider = "openrouter"

    def __init__(self, api_key: str, model: str, *, http: Any = None) -> None:
        self._key = api_key
        self.model = model
        self.http = http

    def __repr__(self) -> str:
        return f"OpenRouterChat(model={self.model!r})"

    def _system(self, system: str) -> dict:
        if self.model.startswith("anthropic/"):
            # OpenRouter passes Anthropic cache breakpoints through in content parts.
            return {
                "role": "system",
                "content": [{"type": "text", "text": system, "cache_control": _CACHE}],
            }
        return {"role": "system", "content": system}

    def build_body(
        self, system: str, messages: list[dict], tools: list[ToolSpec], max_tokens: int
    ) -> dict:
        out: list[dict] = [self._system(system)]
        for msg in messages:
            role = msg["role"]
            if role == "user":
                out.append({"role": "user", "content": msg["content"]})
            elif role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": msg.get("text") or None}
                calls = msg.get("tool_calls") or []
                if calls:
                    item["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.args)},
                        }
                        for c in calls
                    ]
                out.append(item)
            elif role == "tool_results":
                for r in msg["results"]:
                    out.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        body: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": out}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.schema,
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"
        body["usage"] = {"include": True}
        return body

    def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ChatReply:
        body = self.build_body(system, messages, tools, max_tokens)
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "X-Title": "windows-mcp",
        }
        data, latency = post_json(
            OPENROUTER_CHAT_URL,
            body,
            headers,
            timeout or config.timeout(),
            provider="openrouter",
            http=self.http,
        )
        choices = data.get("choices") or []
        if not choices:
            raise ModelError("openrouter returned no choices")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        calls = []
        for index, call in enumerate(msg.get("tool_calls") or []):
            fn = call.get("function") or {}
            calls.append(
                ToolCall(
                    call.get("id") or f"call_{index}",
                    fn.get("name", ""),
                    _parse_args(fn.get("arguments")),
                )
            )
        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        norm = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_read_tokens": details.get("cached_tokens", 0) or 0,
            "cache_write_tokens": 0,
        }
        if usage.get("cost") is not None:
            norm["cost_usd"] = usage["cost"]
        message = {"role": "assistant", "text": text, "tool_calls": calls, "provider": "openrouter"}
        return ChatReply(text, calls, norm, latency, choices[0].get("finish_reason"), message)


# ---------------------------------------------------------------------------- Script


class ScriptedChat:
    """Replays canned turns from a JSON file: for tests and keyless desktop e2e runs.

    The file is a list of turns, each ``{"text"?: str, "tool_calls": [{"name", "args"}]}``.
    When the script runs out, the client answers with plain text (which ends a Do run).
    """

    provider = "script"

    def __init__(self, turns: list[dict], model: str = "script", latency_ms: float = 0.0) -> None:
        self.turns = list(turns)
        self.model = model
        self.latency_ms = latency_ms
        self.calls = 0

    @classmethod
    def from_file(cls, path: str) -> "ScriptedChat":
        try:
            turns = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelUnavailable(f"cannot read model script {path!r}: {exc}") from exc
        if not isinstance(turns, list):
            raise ModelUnavailable("model script must be a JSON list of turns")
        return cls(turns, model=path)

    def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ChatReply:
        start = time.perf_counter()
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        index = self.calls
        self.calls += 1
        turn = self.turns[index] if index < len(self.turns) else {"text": "script finished"}
        calls = [
            ToolCall(f"s{index}_{n}", c["name"], dict(c.get("args") or {}))
            for n, c in enumerate(turn.get("tool_calls") or [])
        ]
        text = turn.get("text", "")
        message = {"role": "assistant", "text": text, "tool_calls": calls, "provider": "script"}
        latency = round((time.perf_counter() - start) * 1000, 1)
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        return ChatReply(text, calls, usage, latency, "tool_use" if calls else "end_turn", message)


def get_chat_client(spec: ModelSpec | None, env: dict | None = None, *, http: Any = None):
    """Build the chat client for *spec*, or raise :class:`ModelUnavailable`."""
    if spec is None:
        raise ModelUnavailable("no model configured: WINDOWS_MCP_DO_MODEL is off")
    if spec.provider == "anthropic":
        return AnthropicChat(
            config.api_key("anthropic", env), spec.model, effort=config.effort(env), http=http
        )
    if spec.provider == "openrouter":
        return OpenRouterChat(config.api_key("openrouter", env), spec.model, http=http)
    if spec.provider == "script":
        return ScriptedChat.from_file(spec.model)
    raise ModelUnavailable(f"unsupported provider {spec.provider!r}")
