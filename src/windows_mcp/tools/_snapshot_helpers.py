"""Snapshot / Screenshot shared helpers.

These functions were originally defined in ``__main__.py`` and are now shared
by the Snapshot and Screenshot tool modules.
"""

import io
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from fastmcp.utilities.types import Image
from windows_mcp.desktop.service import Desktop, Size
from windows_mcp.desktop.utils import remove_private_use_chars, repair_surrogates


logger = logging.getLogger(__name__)

MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT = 1920, 1080

SAVE_FORMATS = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".bmp": "BMP",
    ".webp": "WEBP",
}

#: Inline image formats accepted by ``WINDOWS_MCP_IMAGE_FORMAT`` and the ``image_format`` tool
#: argument, mapped to the Pillow encoder name.
IMAGE_FORMATS = {"jpeg": "JPEG", "jpg": "JPEG", "png": "PNG", "webp": "WEBP"}
DEFAULT_IMAGE_FORMAT = "jpeg"
DEFAULT_IMAGE_QUALITY = 80


def _screenshot_scale() -> float:
    value = os.getenv("WINDOWS_MCP_SCREENSHOT_SCALE", "1.0")
    try:
        scale = float(value)
    except ValueError:
        logger.warning("Invalid WINDOWS_MCP_SCREENSHOT_SCALE value %r, using 1.0", value)
        scale = 1.0
    if not (0.1 <= scale <= 1.0):
        logger.warning("WINDOWS_MCP_SCREENSHOT_SCALE %r out of range [0.1, 1.0], clamping", scale)
        scale = max(0.1, min(1.0, scale))
    return scale


def resolve_image_format(value: str | None = None) -> str:
    """Return the canonical inline image format (``"jpeg"``, ``"png"`` or ``"webp"``).

    Args:
        value: A per-call format. When empty, ``WINDOWS_MCP_IMAGE_FORMAT`` is used, and when
            that is unset or invalid, ``jpeg``.

    Returns:
        The canonical format name, used as the MCP image MIME subtype.

    Raises:
        ValueError: If a per-call ``value`` is not a supported format.
    """
    if value is not None and str(value).strip():
        normalized = str(value).strip().lower()
        if normalized not in IMAGE_FORMATS:
            supported = ", ".join(sorted({"jpeg", "png", "webp"}))
            raise ValueError(f"image_format must be one of {supported}, got {value!r}")
        return "jpeg" if normalized == "jpg" else normalized
    env_value = os.getenv("WINDOWS_MCP_IMAGE_FORMAT", "").strip().lower()
    if not env_value:
        return DEFAULT_IMAGE_FORMAT
    if env_value not in IMAGE_FORMATS:
        logger.warning(
            "Invalid WINDOWS_MCP_IMAGE_FORMAT value %r, using %s", env_value, DEFAULT_IMAGE_FORMAT
        )
        return DEFAULT_IMAGE_FORMAT
    return "jpeg" if env_value == "jpg" else env_value


def _image_quality() -> int:
    value = os.getenv("WINDOWS_MCP_IMAGE_QUALITY", "").strip()
    if not value:
        return DEFAULT_IMAGE_QUALITY
    try:
        quality = int(value)
    except ValueError:
        logger.warning(
            "Invalid WINDOWS_MCP_IMAGE_QUALITY value %r, using %d", value, DEFAULT_IMAGE_QUALITY
        )
        return DEFAULT_IMAGE_QUALITY
    if not (1 <= quality <= 100):
        logger.warning("WINDOWS_MCP_IMAGE_QUALITY %r out of range [1, 100], clamping", quality)
        quality = max(1, min(100, quality))
    return quality


def encode_image(image, image_format: str, *, annotated: bool = False) -> bytes:
    """Encode a screenshot for the inline MCP image.

    JPEG is the default because it encodes a 1080p desktop roughly 15x faster than PNG and at
    about half the size. Annotated images keep full chroma (4:4:4) so the thin coloured boxes and
    small labels stay sharp; plain screenshots use 4:2:0. WebP uses ``method=0``, its fastest
    setting. PNG keeps Pillow's default compression, as before.

    Args:
        image: The PIL image to encode.
        image_format: A canonical format from ``resolve_image_format``.
        annotated: Whether the image carries drawn annotations or grid lines.

    Returns:
        The encoded bytes.
    """
    buffered = io.BytesIO()
    if image_format == "png":
        image.save(buffered, format="PNG")
    else:
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        quality = _image_quality()
        if image_format == "webp":
            image.save(buffered, format="WEBP", quality=quality, method=0)
        else:
            image.save(
                buffered,
                format="JPEG",
                quality=quality,
                optimize=False,
                subsampling=0 if annotated else 2,
            )
    return buffered.getvalue()


def _snapshot_profile_enabled() -> bool:
    value = os.getenv("WINDOWS_MCP_PROFILE_SNAPSHOT", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_bool(value: bool | str) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _as_region(value: list | str | None) -> list | None:
    if value is None or isinstance(value, list):
        return value
    return json.loads(value)


def resolve_save_path(save_path: str | None) -> tuple[Path, str] | None:
    """Validate a ``save_path`` argument and resolve the file the image will be written to.

    Args:
        save_path: Absolute path to an image file (``.png``, ``.jpg``/``.jpeg``, ``.bmp`` or
            ``.webp``), or to a directory. A directory is recognized when it already exists or
            the path ends with a path separator; a timestamped ``screenshot-*.png`` name is then
            generated inside it. Missing parent directories are created.

    Returns:
        ``(file_path, pil_format)``, or ``None`` when ``save_path`` is empty.

    Raises:
        ValueError: If the path is relative or has an unsupported extension.
    """
    if save_path is None or not str(save_path).strip():
        return None
    raw = str(save_path).strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"save_path must be an absolute path, got {raw!r}")

    if raw.endswith(("/", "\\")) or path.is_dir():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = path / f"screenshot-{stamp}.png"

    image_format = SAVE_FORMATS.get(path.suffix.lower())
    if image_format is None:
        supported = ", ".join(SAVE_FORMATS)
        raise ValueError(
            f"save_path has unsupported extension {path.suffix or '(none)'!r}; "
            f"use one of {supported}, or pass a directory"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.resolve(), image_format


def save_screenshot(
    image,
    target: tuple[Path, str],
    encoded: bytes | None = None,
    encoded_format: str = "png",
) -> str:
    """Write a captured screenshot to disk and return the absolute path written.

    Args:
        image: The PIL image to save (the same pixels returned inline).
        target: ``(file_path, pil_format)`` as returned by ``resolve_save_path``.
        encoded: Already-encoded data for ``image``; reused when the file's format matches
            ``encoded_format`` so the image is not encoded twice.
        encoded_format: The canonical format of ``encoded`` (``"png"``, ``"jpeg"``, ``"webp"``).

    Returns:
        The absolute file path as a string.
    """
    path, image_format = target
    if encoded and IMAGE_FORMATS.get(encoded_format) == image_format:
        path.write_bytes(encoded)
        return str(path)
    if image_format == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image.save(path, format=image_format)
    return str(path)


def capture_desktop_state(
    desktop: Desktop,
    *,
    use_vision: bool,
    use_dom: bool,
    use_annotation: bool,
    use_ui_tree: bool,
    width_reference_line: int | None,
    height_reference_line: int | None,
    display: list[int] | None,
    region: list[int] | None,
    tool_name: str,
    save_path: str | None = None,
    image_format: str | None = None,
):
    profile_enabled = _snapshot_profile_enabled()
    profile_started_at = time.perf_counter()
    stage_started_at = profile_started_at
    desktop_state_ms = 0.0
    metadata_render_ms = 0.0
    screenshot_encode_ms = 0.0

    if use_dom and not use_ui_tree:
        raise ValueError("use_dom=True requires use_ui_tree=True")

    inline_format = resolve_image_format(image_format)
    save_target = resolve_save_path(save_path)
    if save_target is not None and not use_vision:
        raise ValueError("save_path requires use_vision=True")

    display_indices = Desktop.parse_display_selection(display)

    grid_lines = None
    if width_reference_line and height_reference_line:
        grid_lines = (int(width_reference_line), int(height_reference_line))

    desktop_state = desktop.get_state(
        use_vision=use_vision,
        use_dom=use_dom,
        use_annotation=use_annotation,
        use_ui_tree=use_ui_tree,
        as_bytes=False,
        scale=_screenshot_scale(),
        grid_lines=grid_lines,
        display_indices=display_indices,
        region=region,
        max_image_size=Size(width=MAX_IMAGE_WIDTH, height=MAX_IMAGE_HEIGHT),
    )
    if profile_enabled:
        desktop_state_ms = (time.perf_counter() - stage_started_at) * 1000
        stage_started_at = time.perf_counter()

    semantic_tree = desktop_state.tree_state.semantic_tree_to_string()
    windows = desktop_state.windows_to_string()
    active_window = desktop_state.active_window_to_string()
    active_desktop = str(desktop_state.active_desktop.get("name") or "")
    all_desktops = ", ".join(
        str(desktop.get("name") or "") for desktop in desktop_state.all_desktops
    )
    if profile_enabled:
        metadata_render_ms = (time.perf_counter() - stage_started_at) * 1000
        stage_started_at = time.perf_counter()

    screenshot_bytes = None
    if use_vision and desktop_state.screenshot is not None:
        screenshot_bytes = encode_image(
            desktop_state.screenshot,
            inline_format,
            annotated=use_annotation or grid_lines is not None,
        )
    if profile_enabled:
        screenshot_encode_ms = (time.perf_counter() - stage_started_at) * 1000
        logger.info(
            "%s profile: desktop_state_ms=%.1f metadata_render_ms=%.1f encode_ms=%.1f format=%s bytes=%d total_ms=%.1f use_vision=%s use_dom=%s use_ui_tree=%s use_annotation=%s display=%s",
            tool_name,
            desktop_state_ms,
            metadata_render_ms,
            screenshot_encode_ms,
            inline_format,
            len(screenshot_bytes or b""),
            (time.perf_counter() - profile_started_at) * 1000,
            use_vision,
            use_dom,
            use_ui_tree,
            use_annotation,
            display,
        )

    saved_path = None
    if save_target is not None:
        if desktop_state.screenshot is None:
            raise ValueError("No screenshot was captured, so nothing could be saved to save_path")
        saved_path = save_screenshot(
            desktop_state.screenshot, save_target, screenshot_bytes, inline_format
        )

    return {
        "desktop_state": desktop_state,
        "semantic_tree": semantic_tree,
        "windows": windows,
        "active_window": active_window,
        "active_desktop": active_desktop,
        "all_desktops": all_desktops,
        "screenshot_bytes": screenshot_bytes,
        "image_format": inline_format,
        "use_ui_tree": use_ui_tree,
        "saved_path": saved_path,
    }


def build_snapshot_response(
    capture_result: dict[str, object],
    *,
    include_ui_details: bool,
    ui_detail_note: str | None = None,
):
    desktop_state = capture_result["desktop_state"]
    semantic_tree = capture_result["semantic_tree"]
    windows = capture_result["windows"]
    active_window = capture_result["active_window"]
    active_desktop = capture_result["active_desktop"]
    all_desktops = capture_result["all_desktops"]
    screenshot_bytes = capture_result["screenshot_bytes"]

    # Some applications (e.g. VS Code) embed Unicode Private Use Area characters in the
    # Automation Element Name property of certain UI elements (e.g. navigation bar items in VS Code).
    # These characters can cause display issues, so we strip them out before rendering.
    semantic_tree = remove_private_use_chars(semantic_tree)

    # Emoji reach us from UIA as raw UTF-16 surrogate pairs. Left alone they make
    # the strict UTF-8 JSON encoder reject the entire response, so every string
    # heading into it is repaired -- window titles included, since an emoji in a
    # window title is enough to take the whole snapshot down.
    semantic_tree = repair_surrogates(semantic_tree)
    windows = repair_surrogates(windows)
    active_window = repair_surrogates(active_window)
    active_desktop = repair_surrogates(active_desktop)
    all_desktops = repair_surrogates(all_desktops)

    def display_to_string(display):
        primary = " primary" if display.primary else ""
        return (
            f"{display.index}:{display.device_name} "
            f"{display.bounding_box.xyxy_to_string()}{primary}"
        )

    metadata_text = f"Cursor Position: {desktop_state.cursor_position}\n"
    if desktop_state.screenshot_original_size:
        orig = desktop_state.screenshot_original_size
        scale = desktop_state.screenshot_scale or 1.0
        if scale < 1.0:
            coord_scale = round(1.0 / scale, 6)
            metadata_text += (
                f"Screenshot Original Size: {orig.to_string()}\n"
                f"Screenshot Coordinate Scale: {coord_scale} "
                f"— image pixels are downscaled; multiply every image pixel coordinate by "
                f"{coord_scale} before passing to Click, Move, Scroll, or any loc= argument "
                f"(e.g. image pixel (200, 150) → screen coordinate ({round(200 * coord_scale)}, {round(150 * coord_scale)}))\n"
            )
        else:
            metadata_text += f"Screenshot Size: {orig.to_string()}\n"
    if desktop_state.available_displays:
        metadata_text += "Visible Displays: "
        metadata_text += "; ".join(
            display_to_string(display) for display in desktop_state.available_displays
        )
        metadata_text += "\n"
    if desktop_state.screenshot_displays:
        metadata_text += f"Selected Displays: {','.join(str(index) for index in desktop_state.screenshot_displays)}\n"
    if desktop_state.screenshot_region:
        metadata_text += f"Screenshot Region: {desktop_state.screenshot_region.xyxy_to_string()}\n"
        metadata_text += "Coordinate Space: Virtual desktop coordinates\n"
    if desktop_state.screenshot_backend:
        metadata_text += f"Screenshot Backend: {desktop_state.screenshot_backend}\n"
    saved_path = capture_result.get("saved_path")
    if saved_path:
        metadata_text += f"Screenshot Saved To: {saved_path}\n"
    if ui_detail_note:
        metadata_text += f"{ui_detail_note}\n"

    # One compact block: no tabulate frame around a single desktop name, and no indentation
    # left over from a dedent that the unindented metadata lines defeat.
    desktop_lines = f"Active Desktop: {active_desktop}"
    if all_desktops != active_desktop:
        desktop_lines += f"\nAll Desktops: {all_desktops}"
    sections = [metadata_text.rstrip("\n"), desktop_lines]
    if capture_result.get("use_ui_tree", True):
        # Without the UI tree no window is enumerated, so these would only say "none found".
        sections.append(f"Focused Window:\n{active_window}")
        sections.append(f"Opened Windows:\n{windows}")
    if include_ui_details:
        sections.append(f"UI Tree:\n{semantic_tree or 'No elements found.'}")
    response_text = "\n\n".join(sections)
    response = [response_text]
    if screenshot_bytes:
        response.append(
            Image(data=screenshot_bytes, format=capture_result.get("image_format") or "png")
        )
    return response
