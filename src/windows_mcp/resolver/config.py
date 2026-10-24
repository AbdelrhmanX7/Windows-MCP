"""Environment configuration for the target resolver."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_MIN_CONFIDENCE = 0.7
DEFAULT_VISION_MODEL = "claude-haiku-5-5"
DEFAULT_VISION_TIMEOUT = 3.0
VISION_PROVIDERS = ("anthropic",)


def _env(env: dict | None) -> dict:
    return os.environ if env is None else env


def _float(env: dict | None, name: str, default: float, low: float, high: float) -> float:
    raw = str(_env(env).get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default
    if not low <= value <= high:
        logger.warning("%s=%r out of range %s..%s, using default %s", name, raw, low, high, default)
        return default
    return value


def min_confidence(env: dict | None = None) -> float:
    """``WINDOWS_MCP_RESOLVER_MIN_CONFIDENCE``: below this, never click (0..1)."""
    return _float(env, "WINDOWS_MCP_RESOLVER_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE, 0.0, 1.0)


def vision_provider(env: dict | None = None) -> str | None:
    """``WINDOWS_MCP_RESOLVER_VISION``: tier-2 provider name, or None when off.

    Tier 2 sends a cropped screenshot of the target window to a third-party model,
    so it is strictly opt-in: an API key in the environment is NOT enough.
    """
    raw = str(_env(env).get("WINDOWS_MCP_RESOLVER_VISION", "")).strip().casefold()
    if raw in ("", "0", "off", "false", "no", "none", "disabled"):
        return None
    if raw in ("1", "on", "true", "yes", "enabled"):
        return "anthropic"
    if raw not in VISION_PROVIDERS:
        logger.warning("Unknown WINDOWS_MCP_RESOLVER_VISION=%r; tier 2 disabled", raw)
        return None
    return raw


def vision_model(env: dict | None = None) -> str:
    """``WINDOWS_MCP_RESOLVER_MODEL``: the fast vision model to ask."""
    return str(_env(env).get("WINDOWS_MCP_RESOLVER_MODEL", "")).strip() or DEFAULT_VISION_MODEL


def vision_timeout(env: dict | None = None) -> float:
    """``WINDOWS_MCP_RESOLVER_VISION_TIMEOUT``: seconds for the whole tier-2 request."""
    return _float(env, "WINDOWS_MCP_RESOLVER_VISION_TIMEOUT", DEFAULT_VISION_TIMEOUT, 0.1, 30.0)
