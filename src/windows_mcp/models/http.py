"""Keep-alive HTTP sessions for model providers.

A fresh TLS connection costs 100-250 ms per request; one ``requests.Session`` per thread
keeps the connection open between the steps of a ``Do`` run (and across runs).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import requests

from windows_mcp.models.config import ModelError

_local = threading.local()


def session() -> requests.Session:
    """This thread's pooled session (requests sessions are not safe to share across threads)."""
    current = getattr(_local, "session", None)
    if current is None:
        current = requests.Session()
        _local.session = current
    return current


def _error_detail(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        return (getattr(response, "text", "") or "")[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message[:600]
        return json.dumps(error)[:600]
    if isinstance(error, str):
        return error[:600]
    return json.dumps(body)[:300]


def post_json(
    url: str,
    body: dict,
    headers: dict,
    timeout: float,
    *,
    provider: str,
    http: Any = None,
) -> tuple[dict, float]:
    """POST JSON, return ``(body, latency_ms)``. Errors never include request headers (keys)."""
    client = http if http is not None else session()
    start = time.perf_counter()
    try:
        response = client.post(url, json=body, headers=headers, timeout=timeout)
    except requests.Timeout as exc:
        raise ModelError(f"{provider} request timed out after {timeout:g}s") from exc
    except requests.RequestException as exc:
        raise ModelError(f"{provider} request failed: {type(exc).__name__}") from exc
    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    if response.status_code != 200:
        raise ModelError(
            f"{provider} returned HTTP {response.status_code}: {_error_detail(response)}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise ModelError(f"{provider} returned an unreadable body") from exc
    if not isinstance(data, dict):
        raise ModelError(f"{provider} returned a non-object body")
    return data, latency_ms
