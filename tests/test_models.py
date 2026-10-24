"""Model clients, Decision-1 adapter and router with mocked HTTP (no keys, no network)."""

from __future__ import annotations

import json
import logging

import pytest
import requests

from windows_mcp.models import config
from windows_mcp.models.chat import (
    AnthropicChat,
    OpenRouterChat,
    ScriptedChat,
    ToolCall,
    ToolSpec,
    get_chat_client,
)
from windows_mcp.models.config import ModelError, ModelSpec, ModelUnavailable, parse_spec
from windows_mcp.models.decision import (
    DECISIONS_URL,
    DecisionClient,
    choice,
    is_decision_model,
    noul,
    score,
)
from windows_mcp.models.router import ROUTE_OPTIONS, Router, parse_llm_choice

FAKE_OR = "sk-or-test-NOT-A-REAL-KEY-123"
FAKE_ANT = "sk-ant-test-NOT-A-REAL-KEY-456"

# Recorded from a live call on 2026-10-24 (ids shortened); request had route + verify.
DECISION_RESPONSE = {
    "model": "microsoft/microsoft-decision-1-20261009",
    "answers": {
        "route": {
            "type": "choice",
            "choice": "direct_action",
            "probabilities": {
                "answer_only": 0.00023615124071615915,
                "clarify": 0.00038934757367095775,
                "direct_action": 0.7976875128564855,
                "needs_plan": 0.20168698832912732,
            },
            "confidence": 0.7302500171419807,
        },
    },
    "usage": {"input_tokens": 115, "output_tokens": 2, "cost": 4.83e-06},
    "id": "gen-dec-1792834078-test",
    "provider": "Azure",
}
ZOD_400 = {
    "error": {
        "message": '[{"code": "invalid_union", "path": ["questions", "q"], '
        '"message": "Invalid input"}]',
        "code": 400,
    }
}


class FakeResponse:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeHTTP:
    """Stands in for requests.Session: records posts, replays queued responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------- config


def test_specs_and_defaults():
    assert config.do_model({}) == ModelSpec("anthropic", "claude-haiku-5-5")
    assert config.router_model({}) == ModelSpec("openrouter", "microsoft/microsoft-decision-1")
    assert config.do_model({"WINDOWS_MCP_DO_MODEL": "off"}) is None
    assert parse_spec("openrouter:anthropic/claude-haiku-5.5").model == "anthropic/claude-haiku-5.5"
    assert parse_spec(r"script:C:\x\turns.json").model == r"C:\x\turns.json"
    with pytest.raises(ModelUnavailable):
        parse_spec("gpt-5")
    with pytest.raises(ModelUnavailable):
        parse_spec("mystery:model")
    assert config.timeout({"WINDOWS_MCP_MODEL_TIMEOUT": "abc"}) == config.DEFAULT_TIMEOUT
    assert config.effort({}) == "low"
    assert config.effort({"WINDOWS_MCP_DO_EFFORT": "off"}) is None


def test_missing_key_is_a_clear_no_model_error():
    with pytest.raises(
        ModelUnavailable, match="no model configured: anthropic needs ANTHROPIC_API_KEY"
    ):
        get_chat_client(config.do_model({}), {})
    with pytest.raises(ModelUnavailable, match="OPENROUTER_API_KEY"):
        Router.from_env({})
    with pytest.raises(ModelUnavailable, match="off"):
        Router.from_env({"WINDOWS_MCP_ROUTER_MODEL": "off"})


# ---------------------------------------------------------------------------- Anthropic

TOOLS = [ToolSpec("Click", "Click a thing.", {"type": "object", "properties": {}})]


def _anthropic_reply(content, usage=None):
    return FakeResponse(
        200,
        {
            "content": content,
            "stop_reason": "tool_use",
            "usage": usage
            or {
                "input_tokens": 12,
                "output_tokens": 30,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 0,
            },
        },
    )


def test_anthropic_body_caches_and_sends_no_sampling_params():
    chat = AnthropicChat(FAKE_ANT, "claude-haiku-5-5", effort="low")
    history = [
        {"role": "user", "content": "Goal: x"},
        {
            "role": "assistant",
            "text": "",
            "tool_calls": [ToolCall("t1", "Click", {"target": "OK"})],
            "raw": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "tool_use", "id": "t1", "name": "Click", "input": {"target": "OK"}},
            ],
            "provider": "anthropic",
        },
        {"role": "tool_results", "results": [{"id": "t1", "name": "Click", "content": "ok"}]},
    ]
    body = chat.build_body("SYSTEM", history, TOOLS, 512)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"] == {"effort": "low"}
    assert body["tool_choice"] == {"type": "auto"}
    for banned in ("temperature", "top_p", "top_k", "thinking"):
        assert banned not in body
    # Thinking block replayed unchanged; rolling breakpoint only on the newest block.
    assert body["messages"][1]["content"][0]["type"] == "thinking"
    assert body["messages"][2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in body["messages"][0]["content"][-1]
    # History itself was not mutated.
    assert "cache_control" not in json.dumps(history, default=str)


def test_anthropic_chat_parses_tool_calls_and_usage():
    http = FakeHTTP(
        _anthropic_reply(
            [
                {"type": "thinking", "thinking": "", "signature": "s"},
                {"type": "text", "text": "Opening."},
                {"type": "tool_use", "id": "tu1", "name": "App", "input": {"name": "Notepad"}},
            ]
        )
    )
    chat = AnthropicChat(FAKE_ANT, "claude-haiku-5-5", http=http)
    reply = chat.chat("SYS", [{"role": "user", "content": "hi"}], TOOLS, timeout=3)
    assert reply.text == "Opening."
    assert [(c.name, c.args) for c in reply.tool_calls] == [("App", {"name": "Notepad"})]
    assert reply.usage["cache_read_tokens"] == 900
    assert reply.message["raw"][0]["type"] == "thinking"
    post = http.posts[0]
    assert post["url"].endswith("/v1/messages") and post["headers"]["x-api-key"] == FAKE_ANT
    assert post["timeout"] == 3


def test_errors_and_repr_never_leak_keys(caplog):
    caplog.set_level(logging.DEBUG)
    http = FakeHTTP(
        FakeResponse(401, {"error": {"message": "invalid x-api-key"}}),
        requests.ConnectionError(f"boom {FAKE_ANT}"),
    )
    chat = AnthropicChat(FAKE_ANT, "claude-haiku-5-5", http=http)
    assert FAKE_ANT not in repr(chat)
    with pytest.raises(ModelError) as first:
        chat.chat("s", [{"role": "user", "content": "x"}], [])
    with pytest.raises(ModelError) as second:
        chat.chat("s", [{"role": "user", "content": "x"}], [])
    assert "HTTP 401" in str(first.value)
    for exc in (first.value, second.value):
        assert FAKE_ANT not in str(exc)
    router = Router(
        ModelSpec("openrouter", "microsoft/microsoft-decision-1"), {"OPENROUTER_API_KEY": FAKE_OR}
    )
    assert FAKE_OR not in repr(router) and FAKE_OR not in repr(router._decider)
    assert FAKE_ANT not in caplog.text and FAKE_OR not in caplog.text


def test_timeout_becomes_model_error():
    http = FakeHTTP(requests.Timeout("slow"))
    chat = AnthropicChat(FAKE_ANT, "m", http=http)
    with pytest.raises(ModelError, match="timed out"):
        chat.chat("s", [{"role": "user", "content": "x"}], [], timeout=0.5)


# ---------------------------------------------------------------------------- OpenRouter


def test_openrouter_chat_round_trip():
    http = FakeHTTP(
        FakeResponse(
            200,
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "Type",
                                        "arguments": '{"text": "milk", "target": "Text editor"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 9, "cost": 0.00002},
            },
        )
    )
    chat = OpenRouterChat(FAKE_OR, "anthropic/claude-haiku-5.5", http=http)
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "text": "", "tool_calls": [ToolCall("c0", "App", {"name": "N"})]},
        {"role": "tool_results", "results": [{"id": "c0", "name": "App", "content": "ok"}]},
    ]
    reply = chat.chat("SYS", history, TOOLS)
    assert reply.tool_calls[0].args == {"text": "milk", "target": "Text editor"}
    assert reply.usage["cost_usd"] == 0.00002
    body = http.posts[0]["json"]
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"name": "N"}'
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c0", "content": "ok"}
    assert body["tools"][0]["function"]["name"] == "Click"
    assert "temperature" not in body
    assert http.posts[0]["headers"]["Authorization"] == f"Bearer {FAKE_OR}"


def test_scripted_chat(tmp_path):
    path = tmp_path / "turns.json"
    path.write_text(json.dumps([{"tool_calls": [{"name": "Shortcut", "args": {"shortcut": "a"}}]}]))
    chat = get_chat_client(parse_spec(f"script:{path}"), {})
    assert isinstance(chat, ScriptedChat)
    first = chat.chat("s", [], [])
    assert first.tool_calls[0].name == "Shortcut"
    exhausted = chat.chat("s", [], []).tool_calls
    assert [(c.name, c.args["success"]) for c in exhausted] == [("Done", False)]
    with pytest.raises(ModelUnavailable):
        get_chat_client(parse_spec(f"script:{tmp_path / 'missing.json'}"), {})


# ---------------------------------------------------------------------------- Decision-1


def test_decision_question_builders():
    assert choice("Q?", {"a": "A"}) == {
        "type": "choice",
        "instructions": "Q?",
        "criteria": {"a": "A"},
    }
    assert noul("Bug?")["type"] == "noul"
    with pytest.raises(ValueError):
        noul("Bug?", {"yes": "y"})
    assert score("Urgency?", ["low", "high"])["criteria"] == ["low", "high"]
    assert is_decision_model("microsoft/microsoft-decision-1")
    assert not is_decision_model("anthropic/claude-haiku-5.5")


def test_decision_client_request_and_response_shape():
    http = FakeHTTP(FakeResponse(200, DECISION_RESPONSE))
    client = DecisionClient(FAKE_OR, http=http)
    result = client.decide("open notepad", {"route": choice("How?", ROUTE_OPTIONS)}, timeout=4)
    post = http.posts[0]
    assert post["url"] == DECISIONS_URL == "https://openrouter.ai/api/alpha/decisions"
    assert post["json"] == {
        "model": "microsoft/microsoft-decision-1",
        "state": "open notepad",
        "questions": {
            "route": {"type": "choice", "instructions": "How?", "criteria": ROUTE_OPTIONS}
        },
    }
    assert result.answers["route"]["choice"] == "direct_action"
    assert result.usage["cost"] == 4.83e-06 and result.provider == "Azure"


def test_decision_400_surfaces_zod_message_and_missing_answers():
    http = FakeHTTP(FakeResponse(400, ZOD_400), FakeResponse(200, {"answers": {}}))
    client = DecisionClient(FAKE_OR, http=http)
    with pytest.raises(ModelError, match="HTTP 400: .*invalid_union"):
        client.decide("x", {"q": choice("Q", {"a": "A"})})
    with pytest.raises(ModelError, match="did not answer: q"):
        client.decide("x", {"q": choice("Q", {"a": "A"})})


def test_router_route_and_verify_via_decision_model():
    answer = {"q": {**DECISION_RESPONSE["answers"]["route"]}}
    verify = {
        "q": {
            "type": "choice",
            "choice": "still_loading",
            "probabilities": {"succeeded": 0.2, "failed": 0.1, "still_loading": 0.7},
            "confidence": 0.6,
        }
    }
    http = FakeHTTP(
        FakeResponse(200, {**DECISION_RESPONSE, "answers": answer}),
        FakeResponse(200, {**DECISION_RESPONSE, "answers": verify}),
    )
    router = Router.from_env({"OPENROUTER_API_KEY": FAKE_OR}, http=http)
    decision = router.route("open notepad and type milk")
    assert decision.choice == "direct_action"
    assert abs(sum(decision.probabilities.values()) - 1) < 1e-9
    assert decision.model.startswith("microsoft/microsoft-decision-1")
    check = router.verify("desktop", "Notepad - Untitled", "Notepad open")
    assert check.choice == "still_loading"
    state = http.posts[1]["json"]["state"]
    assert state == {"expected": "Notepad open", "before": "desktop", "after": "Notepad - Untitled"}
    assert set(http.posts[1]["json"]["questions"]["q"]["criteria"]) == {
        "succeeded",
        "failed",
        "still_loading",
    }


def test_router_with_chat_model_parses_json():
    http = FakeHTTP(
        _anthropic_reply(
            [
                {
                    "type": "text",
                    "text": '{"choice": "answer_only", "probabilities": '
                    '{"answer_only": 0.9, "direct_action": 0.1}}',
                }
            ]
        )
    )
    router = Router(
        ModelSpec("anthropic", "claude-haiku-5-5"), {"ANTHROPIC_API_KEY": FAKE_ANT}, http=http
    )
    decision = router.route("what's the capital of France?")
    assert decision.choice == "answer_only"
    assert decision.probabilities["answer_only"] == pytest.approx(0.9)
    assert decision.probabilities["clarify"] == 0
    body = http.posts[0]["json"]
    assert "tools" not in body and "answer_only" in body["system"][0]["text"]


def test_parse_llm_choice_is_tolerant():
    labels = list(ROUTE_OPTIONS)
    assert parse_llm_choice('Sure: {"choice": "clarify"}', labels)[0] == "clarify"
    picked, probs = parse_llm_choice("needs_plan", labels)
    assert picked == "needs_plan" and probs["needs_plan"] == 1.0
    with pytest.raises(ModelError):
        parse_llm_choice("no idea", labels)
