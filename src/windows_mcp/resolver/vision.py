"""Tier 2: ask a fast vision model to pick a numbered box (Set-of-Mark).

PRIVACY: this is the only part of the resolver that sends anything off the
machine. It runs only when ``WINDOWS_MCP_RESOLVER_VISION`` names a provider (an API
key alone never enables it), and it sends only a downscaled JPEG of the target
window plus the target text.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
from dataclasses import dataclass
from typing import Protocol

import requests

from windows_mcp.resolver import config

logger = logging.getLogger(__name__)

MAX_IMAGE_WIDTH = 1024
JPEG_QUALITY = 70
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class VisionUnavailable(RuntimeError):
    """Tier 2 is off, misconfigured, or failed; the caller falls back gracefully."""


@dataclass
class Box:
    """A numbered region drawn on the screenshot, in screen coordinates."""

    number: int
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2


class VisionProvider(Protocol):
    """Pick one box for ``target``; return its number or None for "none"."""

    name: str

    def pick(self, target: str, jpeg: bytes, box_count: int, timeout: float) -> int | None: ...


def build_prompt(target: str, box_count: int) -> str:
    return (
        "This is a screenshot of one application window. Numbered boxes (0 to "
        f"{box_count - 1}) mark clickable regions; each number sits at a box's top-right "
        f'corner. The user wants to click: "{target}".\n'
        "Reply with only the number of the box to click, or the word none if no box "
        "matches. No other text."
    )


def parse_pick(text: str, box_count: int) -> int | None:
    """Extract a box number from a model reply; None for "none" or nonsense."""
    cleaned = text.strip().casefold()
    if not cleaned or cleaned.startswith("none"):
        return None
    match = re.search(r"-?\d+", cleaned)
    if not match:
        return None
    number = int(match.group())
    if 0 <= number < box_count:
        return number
    return None


class AnthropicProvider:
    """Claude via the Messages API, called with plain ``requests`` (no SDK)."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def pick(self, target: str, jpeg: bytes, box_count: int, timeout: float) -> int | None:
        body = {
            "model": self.model,
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": base64.b64encode(jpeg).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": build_prompt(target, box_count)},
                    ],
                }
            ],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        try:
            response = requests.post(ANTHROPIC_URL, json=body, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            raise VisionUnavailable(f"anthropic request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise VisionUnavailable(f"anthropic returned HTTP {response.status_code}")
        try:
            blocks = response.json().get("content", [])
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except (ValueError, AttributeError) as exc:
            raise VisionUnavailable("anthropic returned an unreadable body") from exc
        return parse_pick(text, box_count)


def get_provider(env: dict | None = None) -> VisionProvider:
    """Build the configured provider or raise :class:`VisionUnavailable`.

    Add OpenAI/Gemini by implementing :class:`VisionProvider` and branching here.
    """
    source = os.environ if env is None else env
    name = config.vision_provider(source)
    if name is None:
        raise VisionUnavailable("tier 2 disabled (set WINDOWS_MCP_RESOLVER_VISION=anthropic)")
    if name == "anthropic":
        key = str(source.get("ANTHROPIC_API_KEY", "")).strip()
        if not key:
            raise VisionUnavailable(
                "WINDOWS_MCP_RESOLVER_VISION=anthropic but no ANTHROPIC_API_KEY"
            )
        return AnthropicProvider(key, config.vision_model(source))
    raise VisionUnavailable(f"unsupported vision provider {name!r}")


def grid_boxes(
    left: int, top: int, right: int, bottom: int, cols: int = 8, rows: int = 6
) -> list[Box]:
    """Fallback regions for windows that expose (almost) no accessible elements."""
    width, height = right - left, bottom - top
    boxes: list[Box] = []
    for row in range(rows):
        for col in range(cols):
            boxes.append(
                Box(
                    number=len(boxes),
                    left=left + width * col // cols,
                    top=top + height * row // rows,
                    right=left + width * (col + 1) // cols,
                    bottom=top + height * (row + 1) // rows,
                )
            )
    return boxes


def encode_jpeg(image, max_width: int = MAX_IMAGE_WIDTH, quality: int = JPEG_QUALITY) -> bytes:
    """Downscale a PIL image to ``max_width`` and encode it as JPEG."""
    from PIL import Image

    if image.width > max_width:
        ratio = max_width / image.width
        image = image.resize((max_width, max(1, int(image.height * ratio))), Image.LANCZOS)
    if image.mode != "RGB":
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def annotated_window_jpeg(
    desktop, boxes: list[Box], window_rect: tuple[int, int, int, int]
) -> bytes:
    """Screenshot only the window, with Snapshot's numbered-box drawing, as JPEG."""
    import windows_mcp.uia as uia
    from windows_mcp.tree.views import BoundingBox, TreeElementNode

    left, top, right, bottom = window_rect
    nodes = []
    for box in boxes:
        bbox = BoundingBox(
            left=box.left,
            top=box.top,
            right=box.right,
            bottom=box.bottom,
            width=box.right - box.left,
            height=box.bottom - box.top,
        )
        nodes.append(TreeElementNode(bounding_box=bbox, center=bbox.get_center()))
    image = desktop.get_annotated_screenshot(
        nodes=nodes, capture_rect=uia.Rect(left, top, right, bottom)
    )
    return encode_jpeg(image)


def pick_box(
    desktop,
    target: str,
    boxes: list[Box],
    window_rect: tuple[int, int, int, int],
    *,
    provider: VisionProvider | None = None,
    timeout: float | None = None,
) -> Box | None:
    """Run tier 2 end to end. Raises :class:`VisionUnavailable` on any failure."""
    if not boxes:
        raise VisionUnavailable("no regions to choose from")
    provider = provider or get_provider()
    timeout = config.vision_timeout() if timeout is None else timeout
    try:
        jpeg = annotated_window_jpeg(desktop, boxes, window_rect)
    except Exception as exc:
        raise VisionUnavailable(f"screenshot failed: {exc}") from exc
    number = provider.pick(target, jpeg, len(boxes), timeout)
    if number is None:
        return None
    return boxes[number]
