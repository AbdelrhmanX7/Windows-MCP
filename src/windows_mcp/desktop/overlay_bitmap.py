"""Shared pixel conversion for the control indicator and capture flash."""

from PIL import Image


def premultiplied_bgra(rgba_image: Image.Image, intensity: float) -> bytes:
    """Convert a PIL RGBA image to scaled, premultiplied BGRA bytes.

    The arithmetic is integer and floors, exactly like the per-pixel definition
    ``a' = a * int(intensity * 255) // 255`` and ``c' = c * a' // 255`` (fully transparent
    pixels get zero colour), but it runs vectorised: a full-screen 1920x1080 bitmap takes
    milliseconds instead of the ~0.5 s a Python loop spends holding the GIL.

    Args:
        rgba_image: Source RGBA bitmap.
        intensity: Opacity multiplier for the layered window.

    Returns:
        Premultiplied BGRA pixels in row-major order.
    """
    import numpy

    raw = rgba_image.tobytes("raw", "BGRA")
    if not raw:
        return b""
    pixels = numpy.frombuffer(raw, dtype=numpy.uint8).reshape(-1, 4).astype(numpy.uint16)
    alpha = pixels[:, 3]
    if intensity < 1.0:
        alpha = alpha * int(intensity * 255) // 255
    out = numpy.empty(pixels.shape, dtype=numpy.uint8)
    out[:, :3] = pixels[:, :3] * alpha[:, None] // 255
    out[:, 3] = alpha
    return out.tobytes()
