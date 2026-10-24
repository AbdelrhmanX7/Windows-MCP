"""Which model each role uses: ``provider:model`` specs read from the environment.

Pure Python (no Windows imports) so the benchmark script can use it on any host.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "openrouter", "script")
DEFAULT_DO_MODEL = "anthropic:claude-haiku-5-5"
DEFAULT_ROUTER_MODEL = "openrouter:microsoft/microsoft-decision-1"
DEFAULT_TIMEOUT = 15.0
DEFAULT_EFFORT = "low"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
_OFF = {"0", "off", "false", "no", "none", "disabled"}


class ModelUnavailable(RuntimeError):
    """No usable model for a role (unset, disabled, unknown provider or missing key)."""


class ModelError(RuntimeError):
    """A configured model failed (HTTP error, timeout, unreadable reply)."""


@dataclass(frozen=True)
class ModelSpec:
    """One model: ``provider`` (anthropic, openrouter, script) and its model id or path."""

    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


def _env(env: dict | None) -> dict:
    return os.environ if env is None else env


def parse_spec(raw: str) -> ModelSpec:
    """Parse ``provider:model``. Only the first colon splits, so ``script:C:\\x.json`` works."""
    text = str(raw or "").strip()
    provider, sep, model = text.partition(":")
    provider = provider.strip().casefold()
    model = model.strip()
    if not sep or not model:
        raise ModelUnavailable(f"model spec {text!r} must look like provider:model")
    if provider not in PROVIDERS:
        raise ModelUnavailable(
            f"unknown model provider {provider!r} in {text!r} (use one of {', '.join(PROVIDERS)})"
        )
    return ModelSpec(provider, model)


def _role_spec(env: dict | None, name: str, default: str) -> ModelSpec | None:
    raw = str(_env(env).get(name, "")).strip()
    if raw.casefold() in _OFF:
        return None
    return parse_spec(raw or default)


def do_model(env: dict | None = None) -> ModelSpec | None:
    """``WINDOWS_MCP_DO_MODEL``: the inner agent model for ``Do`` (None when set to off)."""
    return _role_spec(env, "WINDOWS_MCP_DO_MODEL", DEFAULT_DO_MODEL)


def router_model(env: dict | None = None) -> ModelSpec | None:
    """``WINDOWS_MCP_ROUTER_MODEL``: the scorer for ``Route`` and Do's step verifier."""
    return _role_spec(env, "WINDOWS_MCP_ROUTER_MODEL", DEFAULT_ROUTER_MODEL)


def api_key(provider: str, env: dict | None = None) -> str:
    """The provider's key from the environment, or raise :class:`ModelUnavailable`."""
    name = KEY_ENV.get(provider)
    if name is None:
        return ""
    key = str(_env(env).get(name, "")).strip()
    if not key:
        raise ModelUnavailable(f"no model configured: {provider} needs {name}")
    return key


def timeout(env: dict | None = None) -> float:
    """``WINDOWS_MCP_MODEL_TIMEOUT``: seconds per model request (0.5..120, default 15)."""
    raw = str(_env(env).get("WINDOWS_MCP_MODEL_TIMEOUT", "")).strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid WINDOWS_MCP_MODEL_TIMEOUT=%r, using %s", raw, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    if not 0.5 <= value <= 120:
        logger.warning("WINDOWS_MCP_MODEL_TIMEOUT=%r out of range, using %s", raw, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    return value


def effort(env: dict | None = None) -> str | None:
    """``WINDOWS_MCP_DO_EFFORT``: Anthropic ``output_config.effort`` (default low; off omits it)."""
    raw = str(_env(env).get("WINDOWS_MCP_DO_EFFORT", "")).strip().casefold()
    if raw in _OFF:
        return None
    if not raw:
        return DEFAULT_EFFORT
    if raw not in EFFORTS:
        logger.warning("Unknown WINDOWS_MCP_DO_EFFORT=%r, using %s", raw, DEFAULT_EFFORT)
        return DEFAULT_EFFORT
    return raw
