from dataclasses import dataclass
import ctypes
import logging
import os
from typing import Any
from _ctypes import COMError

from PIL import Image, ImageGrab

import windows_mcp.uia as uia

logger = logging.getLogger(__name__)

#: Sentinel for "dxcam not imported yet". dxcam pulls in numpy and enumerates every DXGI
#: adapter at import time (~200 ms), so it is loaded on the first capture that can use it
#: instead of at server startup. ``None`` means the import failed (no DXGI, session 0, ...).
_DXCAM_UNLOADED: Any = object()
dxcam: Any = _DXCAM_UNLOADED


def _load_dxcam() -> Any:
    """Import dxcam on first use and return the module, or ``None`` when it is unusable."""
    global dxcam
    if dxcam is _DXCAM_UNLOADED:
        try:
            import dxcam as module
        except Exception:
            logger.debug("dxcam is unavailable; DXGI capture disabled", exc_info=True)
            module = None
        dxcam = module
    return dxcam


try:
    import mss
except ImportError:
    mss = None


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DxcamOutput:
    device_idx: int
    output_idx: int
    rect: uia.Rect


def _build_crop_box(capture_rect: uia.Rect, padding: int = 0) -> tuple[int, int, int, int]:
    left_offset, top_offset, _, _ = uia.GetVirtualScreenRect()
    return (
        capture_rect.left - left_offset + padding,
        capture_rect.top - top_offset + padding,
        capture_rect.right - left_offset + padding,
        capture_rect.bottom - top_offset + padding,
    )


def _crop_screenshot(screenshot: Image.Image, capture_rect: uia.Rect | None) -> Image.Image:
    if capture_rect is None:
        return screenshot
    return screenshot.crop(_build_crop_box(capture_rect))


def _virtual_screen_rect() -> uia.Rect:
    left, top, width, height = uia.GetVirtualScreenRect()
    return uia.Rect(left, top, left + width, top + height)


def wait_for_composition() -> None:
    """Block until the desktop compositor has presented a frame (``DwmFlush``).

    Called after an overlay was hidden for capture: hiding a window returns before DWM has
    composed a frame without it, and DXGI desktop duplication hands back the latest *presented*
    frame. Waiting for the next present (at most one refresh interval) guarantees the pixels
    read afterwards no longer contain the hidden overlay. Failures are ignored; DWM is always on
    in Windows 8 and later.
    """
    try:
        ctypes.windll.dwmapi.DwmFlush()
    except Exception:
        logger.debug("DwmFlush failed", exc_info=True)


def get_screenshot_backend() -> str:
    """Read the preferred backend from the environment variable."""
    value = os.getenv("WINDOWS_MCP_SCREENSHOT_BACKEND", "auto")
    normalized = value.strip().lower()
    valid = _ScreenshotBackend.registry.keys() | {"auto"}
    if normalized in valid:
        return normalized
    logger.warning(
        "Unknown screenshot backend '%s'; falling back to auto",
        value,
    )
    return "auto"


# ---------------------------------------------------------------------------
# Backend framework
# ---------------------------------------------------------------------------


class _ScreenshotBackend:
    """Base class for screenshot capture backends.

    Subclasses **must** define two class attributes:

    * ``name: str`` – unique key such as ``"dxcam"``.
    * ``priority: int`` – lower numbers are tried first in the *auto* chain.

    Defining both attributes automatically registers the subclass via
    ``__init_subclass__``.
    """

    name: str
    priority: int

    registry: dict[str, type["_ScreenshotBackend"]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "name" in cls.__dict__ and "priority" in cls.__dict__:
            existing = _ScreenshotBackend.registry.get(cls.name)
            if existing is not None and existing is not cls:
                raise ValueError(f"Duplicate screenshot backend name: {cls.name!r}")
            _ScreenshotBackend.registry[cls.name] = cls

    def is_available(self, capture_rect: uia.Rect | None) -> bool:
        """Return ``True`` if this backend can service the request."""
        return True

    def capture(self, capture_rect: uia.Rect | None) -> Image.Image:
        """Capture a screenshot.  Subclasses must override."""
        raise NotImplementedError


class _DxcamBackend(_ScreenshotBackend):
    """DXGI-based capture via the *dxcam* library.

    Frames are requested as raw BGRA, which needs no colour conversion inside dxcam (its RGB
    path wants OpenCV or a compiled kernel that needs the OpenMP runtime, and fails without
    them); Pillow decodes BGRA to RGB in C. The whole output is always grabbed and sub-regions
    are cropped here, because dxcam only falls back to its last frame (on a static desktop,
    when DXGI has no new frame) for the exact region it captured last.
    """

    name = "dxcam"
    priority = 10

    #: Consecutive capture failures after which dxcam is retired for the process.
    _MAX_FAILURES = 3

    def __init__(self) -> None:
        self._camera_cache: dict[tuple[int, int], object] = {}
        self._failures = 0

    @staticmethod
    def _iter_outputs() -> list[DxcamOutput]:
        if _load_dxcam() is None:
            return []

        factory = getattr(dxcam, "__factory", None)
        if factory is None:
            return []

        outputs: list[DxcamOutput] = []
        for device_idx, device_outputs in enumerate(getattr(factory, "outputs", [])):
            for output_idx, output in enumerate(device_outputs):
                try:
                    output.update_desc()
                    coordinates = output.desc.DesktopCoordinates
                    if not output.attached_to_desktop:
                        continue
                except AttributeError, OSError, RuntimeError, ValueError, COMError:
                    logger.debug(
                        "Failed to read DXGI output geometry for device=%s output=%s",
                        device_idx,
                        output_idx,
                        exc_info=True,
                    )
                    continue
                outputs.append(
                    DxcamOutput(
                        device_idx=device_idx,
                        output_idx=output_idx,
                        rect=uia.Rect(
                            coordinates.left,
                            coordinates.top,
                            coordinates.right,
                            coordinates.bottom,
                        ),
                    )
                )
        return outputs

    @classmethod
    def _resolve_output(cls, capture_rect: uia.Rect) -> DxcamOutput | None:
        """Return the DXGI output that fully contains ``capture_rect``, if any."""
        for output in cls._iter_outputs():
            output_rect = output.rect
            if (
                output_rect.left <= capture_rect.left
                and output_rect.top <= capture_rect.top
                and output_rect.right >= capture_rect.right
                and output_rect.bottom >= capture_rect.bottom
            ):
                return output
        return None

    @classmethod
    def _resolve_region(
        cls,
        capture_rect: uia.Rect,
    ) -> tuple[int, int, tuple[int, int, int, int] | None] | None:
        """Return ``(device_idx, output_idx, region)`` when one DXGI output contains the rect."""
        output = cls._resolve_output(capture_rect)
        if output is None:
            return None
        output_rect = output.rect
        if output_rect == capture_rect:
            return output.device_idx, output.output_idx, None
        return (
            output.device_idx,
            output.output_idx,
            (
                capture_rect.left - output_rect.left,
                capture_rect.top - output_rect.top,
                capture_rect.right - output_rect.left,
                capture_rect.bottom - output_rect.top,
            ),
        )

    def is_available(self, capture_rect: uia.Rect | None) -> bool:
        if _load_dxcam() is None:
            return False
        # A full-desktop request is served when the virtual screen is a single output.
        rect = capture_rect if capture_rect is not None else _virtual_screen_rect()
        return self._resolve_region(rect) is not None

    def _get_camera(self, device_idx: int, output_idx: int) -> object:
        camera_key = (device_idx, output_idx)
        camera = self._camera_cache.get(camera_key)
        if camera is None:
            camera = dxcam.create(
                device_idx=device_idx,
                output_idx=output_idx,
                output_color="BGRA",
            )
            self._camera_cache[camera_key] = camera
        return camera

    def capture(self, capture_rect: uia.Rect | None) -> Image.Image:
        try:
            image = self._capture(capture_rect)
        except ImportError:
            # A broken installation (a missing native dependency) fails the same way on
            # every call; stop paying for it.
            _degraded_backends.add(self.name)
            raise
        except Exception:
            # Anything else may be transient (display change, secure desktop); retire the
            # backend only after repeated consecutive failures.
            self._failures += 1
            if self._failures >= self._MAX_FAILURES:
                _degraded_backends.add(self.name)
            raise
        self._failures = 0
        return image

    def _capture(self, capture_rect: uia.Rect | None) -> Image.Image:
        rect = capture_rect if capture_rect is not None else _virtual_screen_rect()
        output = self._resolve_output(rect)
        if output is None:
            raise ValueError(
                "DXGI capture supports only regions fully contained within one display"
            )
        camera = self._get_camera(output.device_idx, output.output_idx)
        frame = camera.grab(copy=False, new_frame_only=False)
        if frame is None:
            raise RuntimeError("DXGI capture returned no frame")
        image = _bgra_frame_to_image(frame)
        # dxcam addresses outputs per adapter, so a stale or mismatched output can hand
        # back another monitor's pixels without raising (issue #416). A frame whose size
        # differs from the output is never the right picture; raise so the chain falls
        # through to a backend that captures by desktop coordinates instead. Empty
        # frames are left to _is_usable_capture, which retires the backend (issue #371).
        output_rect = output.rect
        expected_size = (output_rect.width(), output_rect.height())
        if image.width > 0 and image.height > 0 and image.size != expected_size:
            raise RuntimeError(
                f"DXGI capture returned a {image.width}x{image.height} frame for a "
                f"{expected_size[0]}x{expected_size[1]} region "
                f"(device={output.device_idx}, output={output.output_idx})"
            )
        if image.width > 0 and image.height > 0 and output_rect != rect:
            image = image.crop(
                (
                    rect.left - output_rect.left,
                    rect.top - output_rect.top,
                    rect.right - output_rect.left,
                    rect.bottom - output_rect.top,
                )
            )
        return image


def _bgra_frame_to_image(frame: Any) -> Image.Image:
    """Decode a dxcam ``(height, width, 4)`` BGRA frame into a new RGB image.

    The frame may be dxcam's reusable internal buffer, so its pixels are copied out here.
    """
    shape = getattr(frame, "shape", None)
    if not shape or len(shape) != 3 or shape[2] != 4:
        raise RuntimeError(f"DXGI capture returned an unexpected frame shape {shape!r}")
    height, width = int(shape[0]), int(shape[1])
    if width <= 0 or height <= 0:
        return Image.new("RGB", (0, 0))
    flags = getattr(frame, "flags", None)
    if flags is not None and not flags["C_CONTIGUOUS"]:
        import numpy

        frame = numpy.ascontiguousarray(frame)
    return Image.frombytes("RGB", (width, height), frame, "raw", "BGRX")


class _PillowBackend(_ScreenshotBackend):
    """Capture via PIL *ImageGrab* (always available)."""

    name = "pillow"
    priority = 100

    def capture(self, capture_rect: uia.Rect | None) -> Image.Image:
        grab_kwargs: dict[str, object] = {"all_screens": True}
        if capture_rect is not None:
            grab_kwargs["bbox"] = (
                capture_rect.left,
                capture_rect.top,
                capture_rect.right,
                capture_rect.bottom,
            )
        try:
            screenshot = ImageGrab.grab(**grab_kwargs)
        except OSError, RuntimeError, ValueError:
            if capture_rect is not None:
                logger.warning(
                    "Failed to capture selected region directly, "
                    "falling back to virtual screen crop"
                )
                # Fallback: grab full virtual screen then crop to the requested region.
                return _crop_screenshot(ImageGrab.grab(all_screens=True), capture_rect)
            logger.warning("Failed to capture virtual screen, using primary screen")
            screenshot = ImageGrab.grab()
        # Success path: ImageGrab.grab(bbox=...) already returned the exact region,
        # so no further cropping is needed.
        return screenshot


class _MssBackend(_ScreenshotBackend):
    """Capture via the *mss* library."""

    name = "mss"
    priority = 20

    def is_available(self, capture_rect: uia.Rect | None) -> bool:
        return mss is not None

    def capture(self, capture_rect: uia.Rect | None) -> Image.Image:
        if mss is None:
            raise RuntimeError("mss is not available")
        with mss.mss() as sct:
            if capture_rect is None:
                monitor = sct.monitors[0]
            else:
                monitor = {
                    "left": capture_rect.left,
                    "top": capture_rect.top,
                    "width": capture_rect.right - capture_rect.left,
                    "height": capture_rect.bottom - capture_rect.top,
                }
            raw = sct.grab(monitor)
            image = Image.frombytes("RGB", raw.size, raw.rgb)
        # mss.grab(monitor) already captures exactly the requested region,
        # so no further cropping is needed.
        return image


# ---------------------------------------------------------------------------
# Instance management
# ---------------------------------------------------------------------------

_backend_instances: dict[str, _ScreenshotBackend] = {}

#: Backends that have handed back an unusable frame in this process.
#: On VM/RDP desktops dxcam can initialize successfully and then return empty
#: frames from then on (issue #371). Retrying it on every call just reproduces
#: the same broken capture, so a backend caught doing this is skipped for the
#: rest of the process and the chain moves on to mss.
_degraded_backends: set[str] = set()


def _get_backend(name: str) -> _ScreenshotBackend:
    """Return a cached singleton instance for the given backend *name*."""
    if name not in _backend_instances:
        cls = _ScreenshotBackend.registry.get(name)
        if cls is None:
            raise ValueError(f"Unknown screenshot backend: {name!r}")
        _backend_instances[name] = cls()
    return _backend_instances[name]


def _is_usable_capture(image: Image.Image | None) -> bool:
    """Return True if a captured frame is structurally sound enough to encode.

    A backend that fails by raising is already handled by the chain below. The
    case this catches is the quiet one behind issue #371: on VM/RDP desktops
    dxcam can initialize successfully and then return frames carrying no pixel
    data, which travel all the way to the client as an undecodable image with no
    error anywhere in between.

    Only structure is checked, never content -- a legitimately black screen is a
    perfectly valid screenshot, and rejecting it would break locked and
    screensaver desktops.
    """
    if image is None:
        return False
    if image.width <= 0 or image.height <= 0:
        return False
    try:
        image.load()
    except OSError, ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def warm_up() -> None:
    """Load dxcam and open the desktop-duplication camera ahead of the first capture.

    Run in the background after the server is up, on the UIA worker that later captures, so
    the first Screenshot does not pay for importing dxcam/numpy and creating the camera. Does
    nothing when another backend is forced; never raises.
    """
    if get_screenshot_backend() not in ("auto", "dxcam"):
        return
    try:
        backend = _get_backend("dxcam")
        rect = _virtual_screen_rect()
        if not backend.is_available(rect):
            return
        output = backend._resolve_output(rect)
        if output is not None:
            backend._get_camera(output.device_idx, output.output_idx)
    except Exception:
        logger.debug("Screenshot warm-up failed", exc_info=True)


def capture(
    capture_rect: uia.Rect | None,
    backend: str | None = None,
) -> tuple[Image.Image, str]:
    """Capture a screenshot and return ``(image, backend_name_used)``."""
    selected = backend or get_screenshot_backend()

    # Build the candidate chain: all registered backends sorted by priority, or a single one.
    if selected == "auto":
        chain = sorted(_ScreenshotBackend.registry.values(), key=lambda c: c.priority)
    else:
        cls = _ScreenshotBackend.registry.get(selected)
        if cls is None:
            raise ValueError(f"Unknown screenshot backend: {selected!r}")
        chain = [cls]

    # Try each candidate: skip unavailable ones, catch failures and fall through.
    # Any exception is caught -- the chain exists so that one backend misbehaving
    # on unusual hardware degrades to a working one, and third-party backends
    # (dxcam in particular) raise well outside the usual OSError family (#417).
    failures: list[tuple[str, BaseException]] = []
    for backend_cls in chain:
        if backend_cls.name in _degraded_backends:
            continue
        inst = _get_backend(backend_cls.name)
        try:
            if not inst.is_available(capture_rect):
                continue
            image = inst.capture(capture_rect)
        except Exception as exc:
            failures.append((inst.name, exc))
            logger.warning(
                "Screenshot backend '%s' failed; trying next backend",
                inst.name,
                exc_info=selected != "auto",
            )
            continue

        # A backend can also fail silently, returning a frame with nothing in it.
        # Treat that exactly like a raised failure rather than shipping bytes the
        # client cannot decode.
        if not _is_usable_capture(image):
            _degraded_backends.add(inst.name)
            logger.warning(
                "Screenshot backend '%s' returned an unusable frame; disabling it "
                "for this process and trying the next backend",
                inst.name,
            )
            continue

        return image, inst.name

    # All candidates exhausted — pillow is always present as the last resort.
    try:
        return _get_backend("pillow").capture(capture_rect), "pillow"
    except Exception as exc:
        failures.append(("pillow", exc))
        summary = "; ".join(f"{name}: {type(err).__name__}: {err}" for name, err in failures)
        raise RuntimeError(f"All screenshot backends failed ({summary})") from exc
