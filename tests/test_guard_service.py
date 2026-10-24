"""Guard service (state, security_event emission, gate decision) and the Decision-1 adapter.

No Windows imports and no network: the HTTP layer is a fake ``post`` callable.
"""

import logging
import threading

import pytest

from windows_mcp.guard.model import (
    CRITERIA,
    DEFAULT_URL,
    DecisionClient,
    build_request,
    parse_response,
)
from windows_mcp.guard.service import GuardService, normalize_shortcut
from windows_mcp.tasks.events import TaskEventBus

KEY = "sk-or-test-not-a-real-key"


def ctx(url=None, *, password=False, recipients=None, title="Page", text=None, hwnd=1):
    return {
        "hwnd": hwnd,
        "title": title,
        "process": "msedge.exe" if url else "outlook.exe",
        "app": "browser" if url else "email",
        "url": url,
        "focused": {"name": "Password" if password else "Email", "is_password": password},
        "recipients": recipients or [],
        "page_text": text,
        "ts": 0,
    }


class FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data


def decision(choice, probs):
    return {
        "id": "gen-dec-1",
        "model": "microsoft/microsoft-decision-1-20261009",
        "provider": "Azure",
        "answers": {
            "verdict": {
                "type": "choice",
                "choice": choice,
                "probabilities": probs,
                "confidence": 0.77,
            }
        },
        "usage": {"input_tokens": 83, "output_tokens": 1, "cost": 0.0000035},
    }


class FakePost:
    def __init__(self, response=None, exc=None):
        self.calls = []
        self.response = response
        self.exc = exc
        self.event = threading.Event()

    def __call__(self, url, *, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        self.event.set()
        if self.exc:
            raise self.exc
        return self.response


def disabled_model():
    return DecisionClient(api_key="")


# --------------------------------------------------------------------------- model adapter
def test_request_matches_decisions_schema_and_sends_only_allowed_fields():
    body = build_request(
        "microsoft/microsoft-decision-1",
        domain="rnicorsoft.com",
        title="Sign in to your account",
        excerpt="Sign in   Email, phone, or Skype " * 200,
        source="browser",
    )
    assert body["model"] == "microsoft/microsoft-decision-1"
    assert set(body["state"]) == {"domain", "page_title", "visible_text_excerpt"}
    assert len(body["state"]["visible_text_excerpt"]) <= 1500
    q = body["questions"]["verdict"]
    assert q["type"] == "choice" and q["instructions"]
    assert set(q["criteria"]) == {"legit", "phishing", "tech_support_scam", "unsure"}
    assert q["criteria"] == CRITERIA


def test_parse_response_reads_probabilities_and_falls_back_to_choice():
    out = parse_response(decision("phishing", {"legit": 0.025, "phishing": 0.829, "unsure": 0.1}))
    assert out["choice"] == "phishing" and out["probabilities"]["phishing"] == 0.829
    assert out["probabilities"]["tech_support_scam"] == 0.0
    assert out["model"].startswith("microsoft/")
    bare = parse_response({"answers": {"verdict": {"type": "choice", "choice": "legit"}}})
    assert bare["choice"] == "legit" and bare["probabilities"] == {}
    assert parse_response({"answers": {}}) is None
    assert parse_response({"error": {"message": "bad"}}) is None
    assert parse_response("nope") is None


def test_client_posts_to_decisions_endpoint_with_bearer_and_caches():
    post = FakePost(FakeResponse(decision("phishing", {"phishing": 0.9, "legit": 0.05})))
    client = DecisionClient(api_key=KEY, post=post, timeout=1.5)
    first = client.classify("rnicorsoft.com", title="Sign in", excerpt="Enter password")
    second = client.classify("rnicorsoft.com", title="Sign in", excerpt="Enter password")
    assert first["choice"] == "phishing" and second["choice"] == "phishing"
    assert len(post.calls) == 1  # cached per domain
    call = post.calls[0]
    assert call["url"] == DEFAULT_URL == "https://openrouter.ai/api/alpha/decisions"
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["timeout"] == 1.5
    assert "ms" in first


def test_client_disabled_without_key_and_never_calls(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    post = FakePost(FakeResponse({}))
    client = DecisionClient(post=post)
    assert not client.enabled
    assert client.classify("x.com") is None
    assert client.submit("x.com", lambda r: None) is False
    assert post.calls == []


def test_client_env_configuration(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("WINDOWS_MCP_GUARD_MODEL", "other/model")
    monkeypatch.setenv("WINDOWS_MCP_GUARD_TIMEOUT", "0.7")
    client = DecisionClient()
    assert client.enabled and client.model == "other/model" and client.timeout == 0.7
    assert KEY not in repr(client)


def test_client_failure_returns_none_and_never_logs_key(caplog):
    caplog.set_level(logging.DEBUG)
    post = FakePost(exc=TimeoutError("read timed out"))
    client = DecisionClient(api_key=KEY, post=post)
    assert client.classify("rnicorsoft.com") is None
    assert client.errors == 1
    post_400 = FakePost(FakeResponse({"error": {"message": "zod"}}, status=400))
    assert DecisionClient(api_key=KEY, post=post_400).classify("a.com") is None
    assert KEY not in caplog.text


def test_submit_dedups_inflight_and_calls_back():
    gate = threading.Event()

    class SlowPost(FakePost):
        def __call__(self, url, **kw):
            gate.wait(2)
            return super().__call__(url, **kw)

    post = SlowPost(FakeResponse(decision("phishing", {"phishing": 0.9})))
    client = DecisionClient(api_key=KEY, post=post)
    results = []
    done = threading.Event()

    def cb(r):
        results.append(r)
        if len(results) == 2:
            done.set()

    assert client.submit("evil.example", cb)
    assert client.submit("evil.example", cb)
    gate.set()
    assert done.wait(3)
    assert len(post.calls) == 1 and all(r["choice"] == "phishing" for r in results)


# --------------------------------------------------------------------------- service + events
def test_dangerous_page_emits_security_event_on_the_task_bus():
    bus = TaskEventBus()
    svc = GuardService(publish=bus.publish, model=disabled_model())
    res = svc.evaluate(ctx("https://rnicorsoft.com/login", password=True))
    assert res["verdict"].verdict == "dangerous"
    events = bus.history()
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "security_event" and ev["event"] == "verdict"
    assert ev["seq"] == 1 and isinstance(ev["ts"], int)
    assert ev["verdict"] == "dangerous" and ev["domain"] == "rnicorsoft.com"
    assert ev["brand"] == "Microsoft" and ev["source"] == "browser"
    assert ev["url"] == "https://rnicorsoft.com/login" and ev["password_field"] is True
    assert ev["reasons"]


def test_only_emits_on_change_and_on_return_to_safe():
    events = []
    svc = GuardService(publish=events.append, model=disabled_model())
    svc.evaluate(ctx("https://www.microsoft.com/"))
    assert events == []  # safe and nothing was risky before: quiet in "risky" mode
    svc.evaluate(ctx("https://rnicorsoft.com/"))
    svc.evaluate(ctx("https://rnicorsoft.com/"))
    assert [e["verdict"] for e in events] == ["dangerous"]
    svc.evaluate(ctx("https://www.microsoft.com/"))
    assert [e["verdict"] for e in events] == ["dangerous", "safe"]
    svc.evaluate(ctx("https://github.com/"))
    assert len(events) == 2


def test_events_mode_all(monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_GUARD_EVENTS", "all")
    events = []
    svc = GuardService(publish=events.append, model=disabled_model())
    svc.evaluate(ctx("https://www.microsoft.com/"))
    svc.evaluate(ctx("https://github.com/"))
    assert [e["domain"] for e in events] == ["microsoft.com", "github.com"]


def test_email_recipient_lookalike():
    events = []
    svc = GuardService(publish=events.append, model=disabled_model())
    svc.evaluate(ctx(recipients=["anna@contoso.com", "support@rnicorsoft.com"]))
    ev = events[-1]
    assert ev["source"] == "email" and ev["recipient"] == "support@rnicorsoft.com"
    assert ev["verdict"] == "dangerous" and "url" not in ev


def test_model_upgrades_gray_verdict_asynchronously_and_emits_again():
    post = FakePost(FakeResponse(decision("phishing", {"phishing": 0.83, "legit": 0.03})))
    events = []
    got = threading.Event()

    def publish(e):
        events.append(e)
        if e.get("model"):
            got.set()

    svc = GuardService(publish=publish, model=DecisionClient(api_key=KEY, post=post))
    res = svc.evaluate(ctx("https://shop-checkout-247.example/", password=True, title="Sign in"))
    assert res["verdict"].verdict == "unknown" and res["verdict"].gray  # returned at once
    assert got.wait(3)
    last = events[-1]
    assert last["verdict"] == "dangerous" and last["model"]["choice"] == "phishing"
    body = post.calls[0]["json"]
    assert body["state"]["domain"] == "shop-checkout-247.example"
    assert "url" not in body["state"] and "/" not in body["state"]["domain"]
    _, verdict, _, _ = svc.current()
    assert verdict.verdict == "dangerous"


def test_model_never_lowers_local_verdict():
    post = FakePost(FakeResponse(decision("legit", {"legit": 0.99})))
    svc = GuardService(publish=None, model=DecisionClient(api_key=KEY, post=post))
    svc.evaluate(ctx("https://amazom.com/", title="Amazon"))  # suspicious -> gray
    post.event.wait(2)
    _, verdict, _, _ = svc.current()
    assert verdict.verdict == "suspicious"


def test_safe_and_unknown_pages_never_call_the_model():
    post = FakePost(FakeResponse(decision("legit", {"legit": 0.99})))
    svc = GuardService(publish=None, model=DecisionClient(api_key=KEY, post=post))
    svc.evaluate(ctx("https://login.microsoftonline.com/", password=True))
    svc.evaluate(ctx("https://some-blog.example/post"))
    assert post.calls == []


# --------------------------------------------------------------------------- gate decision
@pytest.mark.parametrize(
    "name,args,gated",
    [
        ("Type", {"text": "hi", "loc": [1, 2]}, True),
        ("MultiEdit", {"locs": [[1, 2, "x"]]}, True),
        ("Click", {"target": "Sign in"}, True),
        ("Click", {"loc": [1, 2]}, False),
        ("Shortcut", {"shortcut": "ctrl+enter"}, True),
        ("Shortcut", {"shortcut": "Alt + S"}, True),
        ("Shortcut", {"shortcut": "ctrl+v"}, True),
        ("Shortcut", {"shortcut": "Enter"}, True),
        ("Shortcut", {"shortcut": "ctrl+t"}, False),
        ("Shortcut", {"shortcut": "alt+tab"}, False),
        ("Type", {"text": "github.com", "target": "Address and search bar"}, False),
        ("Type", {"text": "github.com", "target": "address bar"}, False),
        ("Type", {"text": "me@x.com", "target": "Email address"}, True),
        ("Scroll", {}, False),
        ("Screenshot", {}, False),
    ],
)
def test_gated_calls(name, args, gated):
    assert GuardService.gated_call(name, args) is gated


def test_normalize_shortcut():
    assert normalize_shortcut("Shift+Ctrl+Return") == "ctrl+shift+enter"


def test_risk_for_input_rules():
    svc = GuardService(publish=None, model=disabled_model())
    assert svc.risk_for_input() is None
    svc.evaluate(ctx("https://www.microsoft.com/", password=True))
    assert svc.risk_for_input() is None
    svc.evaluate(ctx("https://rnicorsoft.com/"))
    risk = svc.risk_for_input()
    assert risk["code"] == "SCAM_GUARD_CONFIRM_REQUIRED" and risk["brand"] == "Microsoft"
    assert "confirmed=true" in risk["message"]
    svc.evaluate(ctx("https://amazom.com/"))  # suspicious, no password: allowed
    assert svc.risk_for_input() is None
    svc.evaluate(ctx("https://amazom.com/", password=True))  # -> dangerous
    assert svc.risk_for_input() is not None


def test_confirmed_parsing_is_strict():
    assert GuardService.confirmed({"confirmed": True})
    assert GuardService.confirmed({"confirmed": "true"})
    for value in (False, "yes", "1", 1, None, "false"):
        assert not GuardService.confirmed({"confirmed": value})


def test_override_event():
    events = []
    svc = GuardService(publish=events.append, model=disabled_model())
    svc.evaluate(ctx("https://rnicorsoft.com/"))
    svc.publish_override("Type", svc.risk_for_input())
    assert events[-1]["event"] == "user_confirmed" and events[-1]["tool"] == "Type"


def test_shortcut_in_address_bar_is_not_page_input():
    svc = GuardService(publish=None, model=disabled_model())
    c = ctx("https://rnicorsoft.com/")
    svc.evaluate(c)
    assert svc.risk_for_input("Shortcut") is not None
    c["focused"] = {"name": "Address and search bar", "address_bar": True}
    svc.evaluate(c)
    assert svc.risk_for_input("Shortcut") is None
    assert svc.risk_for_input("Type") is not None
