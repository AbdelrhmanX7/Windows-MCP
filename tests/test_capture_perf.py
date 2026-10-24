"""Fast-path capture behaviour: inline image encoding, overlay hiding and compact output."""

import io
import threading
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from PIL import Image

from windows_mcp.desktop import control_overlay, flash_overlay, overlay_bitmap
from windows_mcp.desktop.service import Desktop
from windows_mcp.desktop.views import DesktopState, Size
from windows_mcp.tools import _snapshot_helpers as helpers
from windows_mcp.tree.views import SemanticNode, TreeState


def _photo_like(size=(64, 48)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    for x in range(size[0]):
        image.putpixel((x, x % size[1]), (255, 0, 0))
    return image


class TestResolveImageFormat:
    def test_default_is_jpeg(self, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_IMAGE_FORMAT", raising=False)
        assert helpers.resolve_image_format() == "jpeg"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("png", "png"), ("PNG", "png"), ("webp", "webp"), ("jpg", "jpeg"), (" jpeg ", "jpeg")],
    )
    def test_env_selects_format(self, monkeypatch, value, expected):
        monkeypatch.setenv("WINDOWS_MCP_IMAGE_FORMAT", value)
        assert helpers.resolve_image_format() == expected

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("WINDOWS_MCP_IMAGE_FORMAT", "gif")
        assert helpers.resolve_image_format() == "jpeg"

    def test_per_call_value_overrides_env(self, monkeypatch):
        monkeypatch.setenv("WINDOWS_MCP_IMAGE_FORMAT", "webp")
        assert helpers.resolve_image_format("png") == "png"

    def test_invalid_per_call_value_raises(self):
        with pytest.raises(ValueError, match="image_format"):
            helpers.resolve_image_format("tiff")


class TestImageQuality:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_IMAGE_QUALITY", raising=False)
        assert helpers._image_quality() == helpers.DEFAULT_IMAGE_QUALITY

    @pytest.mark.parametrize(("value", "expected"), [("55", 55), ("0", 1), ("500", 100)])
    def test_env_is_parsed_and_clamped(self, monkeypatch, value, expected):
        monkeypatch.setenv("WINDOWS_MCP_IMAGE_QUALITY", value)
        assert helpers._image_quality() == expected

    def test_invalid_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("WINDOWS_MCP_IMAGE_QUALITY", "high")
        assert helpers._image_quality() == helpers.DEFAULT_IMAGE_QUALITY


class TestEncodeImage:
    @pytest.mark.parametrize(
        ("image_format", "pil_format"), [("jpeg", "JPEG"), ("png", "PNG"), ("webp", "WEBP")]
    )
    def test_encodes_requested_format(self, image_format, pil_format):
        data = helpers.encode_image(_photo_like(), image_format)
        with Image.open(io.BytesIO(data)) as decoded:
            assert decoded.format == pil_format
            assert decoded.size == (64, 48)

    def test_png_is_lossless(self):
        image = _photo_like()
        with Image.open(io.BytesIO(helpers.encode_image(image, "png"))) as decoded:
            assert decoded.convert("RGB").tobytes() == image.tobytes()

    def test_rgba_input_is_flattened_for_jpeg(self):
        image = Image.new("RGBA", (8, 8), (10, 20, 30, 128))
        with Image.open(io.BytesIO(helpers.encode_image(image, "jpeg"))) as decoded:
            assert decoded.mode == "RGB"

    def test_annotated_jpeg_keeps_full_chroma(self):
        from PIL import JpegImagePlugin

        image = _photo_like()
        plain = Image.open(io.BytesIO(helpers.encode_image(image, "jpeg")))
        annotated = Image.open(io.BytesIO(helpers.encode_image(image, "jpeg", annotated=True)))
        assert JpegImagePlugin.get_sampling(plain) == 2  # 4:2:0
        assert JpegImagePlugin.get_sampling(annotated) == 0  # 4:4:4

    def test_quality_env_changes_jpeg_size(self, monkeypatch):
        image = _photo_like((256, 256))
        monkeypatch.setenv("WINDOWS_MCP_IMAGE_QUALITY", "95")
        high = helpers.encode_image(image, "jpeg")
        monkeypatch.setenv("WINDOWS_MCP_IMAGE_QUALITY", "20")
        low = helpers.encode_image(image, "jpeg")
        assert len(low) < len(high)


class TestSaveScreenshotReuse:
    def test_matching_format_reuses_encoded_bytes(self, tmp_path):
        target = (tmp_path / "shot.jpg", "JPEG")
        path = helpers.save_screenshot(_photo_like(), target, b"encoded-jpeg", "jpeg")
        assert (tmp_path / "shot.jpg").read_bytes() == b"encoded-jpeg"
        assert path == str(tmp_path / "shot.jpg")

    def test_png_target_is_encoded_lossless_even_when_inline_is_jpeg(self, tmp_path):
        image = _photo_like()
        target = (tmp_path / "shot.png", "PNG")
        helpers.save_screenshot(image, target, b"not-a-png", "jpeg")
        with Image.open(tmp_path / "shot.png") as saved:
            assert saved.format == "PNG"
            assert saved.convert("RGB").tobytes() == image.tobytes()


def _state(**overrides) -> DesktopState:
    values = dict(
        active_desktop={"name": "Desktop 1"},
        all_desktops=[{"name": "Desktop 1"}],
        active_window=None,
        windows=[],
        screenshot=_photo_like(),
        cursor_position=(1, 2),
        screenshot_original_size=Size(width=64, height=48),
        tree_state=TreeState(),
    )
    values.update(overrides)
    return DesktopState(**values)


def _capture(state: DesktopState, **kwargs):
    desktop = MagicMock()
    desktop.get_state.return_value = state
    options = dict(
        use_vision=True,
        use_dom=False,
        use_annotation=False,
        use_ui_tree=True,
        width_reference_line=None,
        height_reference_line=None,
        display=None,
        region=None,
        tool_name="test",
    )
    options.update(kwargs)
    return helpers.capture_desktop_state(desktop, **options)


class TestSnapshotResponse:
    def test_inline_image_uses_resolved_format(self, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_IMAGE_FORMAT", raising=False)
        result = helpers.build_snapshot_response(_capture(_state()), include_ui_details=False)
        image = result[1]
        assert image._mime_type == "image/jpeg"
        with Image.open(io.BytesIO(image.data)) as decoded:
            assert decoded.format == "JPEG"

    def test_per_call_png(self):
        result = helpers.build_snapshot_response(
            _capture(_state(), image_format="png"), include_ui_details=False
        )
        assert result[1]._mime_type == "image/png"

    def test_text_is_compact_and_unindented(self):
        text = helpers.build_snapshot_response(_capture(_state()), include_ui_details=True)[0]
        assert "Active Desktop: Desktop 1" in text
        assert "All Desktops" not in text, "a single desktop is not repeated"
        assert "---" not in text.split("Focused Window:")[0]
        assert not any(line.startswith("    ") for line in text.splitlines()[:6])
        assert "Focused Window:" in text and "Opened Windows:" in text and "UI Tree:" in text

    def test_multiple_desktops_are_listed(self):
        state = _state(all_desktops=[{"name": "Desktop 1"}, {"name": "Work"}])
        text = helpers.build_snapshot_response(_capture(state), include_ui_details=False)[0]
        assert "All Desktops: Desktop 1, Work" in text

    def test_screenshot_without_tree_omits_empty_window_sections(self):
        capture = _capture(_state(), use_ui_tree=False)
        text = helpers.build_snapshot_response(capture, include_ui_details=False)[0]
        assert "Focused Window:" not in text
        assert "No windows found" not in text


class TestSemanticTreeRendering:
    def test_untitled_empty_windows_are_not_rendered(self):
        root = SemanticNode(control_type="Desktop", element_type="desktop", name="Desktop")
        root.add_child(SemanticNode(control_type="Window", element_type="window", name=""))
        root.add_child(SemanticNode(control_type="Window", element_type="window", name="Notepad"))
        text = TreeState(semantic_tree_root=root).semantic_tree_to_string()
        assert 'window ""' not in text
        assert [line.strip() for line in text.splitlines()] == ["desktop", '└── window "Notepad"']

    def test_untitled_window_with_children_is_kept(self):
        root = SemanticNode(control_type="Desktop", element_type="desktop", name="Desktop")
        window = SemanticNode(control_type="Window", element_type="window", name="")
        window.add_child(SemanticNode(control_type="Button", element_type="structural", name="x"))
        root.add_child(window)
        assert 'window ""' in TreeState(semantic_tree_root=root).semantic_tree_to_string()

    def test_multiline_names_stay_on_one_line(self):
        root = SemanticNode(control_type="Desktop", element_type="desktop", name="Desktop")
        root.add_child(SemanticNode(control_type="Window", element_type="window", name="One\nTwo"))
        text = TreeState(semantic_tree_root=root).semantic_tree_to_string()
        assert text.splitlines()[-1].strip() == '└── window "One Two"'


class TestPremultipliedBgra:
    @staticmethod
    def _reference(image: Image.Image, intensity: float) -> bytes:
        bgra = bytearray(image.tobytes("raw", "BGRA"))
        scale = int(intensity * 255)
        for i in range(0, len(bgra), 4):
            a = bgra[i + 3] if intensity >= 1.0 else (bgra[i + 3] * scale) // 255
            bgra[i + 3] = a
            for c in range(3):
                bgra[i + c] = (bgra[i + c] * a) // 255 if a else 0
        return bytes(bgra)

    @pytest.mark.parametrize("intensity", [1.0, 0.5, 0.04, 0.0])
    def test_matches_per_pixel_definition(self, intensity):
        import random

        rng = random.Random(7)
        raw = bytes(rng.randrange(256) for _ in range(23 * 17 * 4))
        image = Image.frombytes("RGBA", (23, 17), raw)
        assert overlay_bitmap.premultiplied_bgra(image, intensity) == self._reference(
            image, intensity
        )


class TestFlashState:
    def test_has_active_flash_tracks_overlays(self, monkeypatch):
        monkeypatch.setattr(flash_overlay, "_active_overlay", None)
        monkeypatch.setattr(flash_overlay, "_all_overlays", set())
        assert flash_overlay.has_active_flash() is False
        overlay = flash_overlay._Overlay()
        monkeypatch.setattr(flash_overlay, "_all_overlays", {overlay})
        assert flash_overlay.has_active_flash() is True


class TestCaptureWaitsForComposition:
    @staticmethod
    def _desktop(monkeypatch, *, indicator_hidden, flash_showing):
        events = []

        @contextmanager
        def suspended():
            events.append("hide")
            yield indicator_hidden
            events.append("restore")

        monkeypatch.setattr(control_overlay, "suspend_for_capture", suspended)
        monkeypatch.setattr(flash_overlay, "has_active_flash", lambda: flash_showing)
        monkeypatch.setattr(flash_overlay, "cancel_active_flash", lambda: True)
        monkeypatch.setattr(flash_overlay, "show_capture_flash", lambda rect: None)
        monkeypatch.setattr(
            "windows_mcp.desktop.service.screenshot_capture.wait_for_composition",
            lambda: events.append("dwm flush"),
        )
        monkeypatch.setattr(
            "windows_mcp.desktop.service.screenshot_capture.capture",
            lambda rect: (events.append("capture") or Image.new("RGB", (2, 2)), "test"),
        )
        Desktop.__new__(Desktop).get_screenshot()
        return events

    def test_flush_after_hiding_indicator(self, monkeypatch):
        events = self._desktop(monkeypatch, indicator_hidden=True, flash_showing=False)
        assert events == ["hide", "dwm flush", "capture", "restore"]

    def test_flush_after_cancelling_flash(self, monkeypatch):
        events = self._desktop(monkeypatch, indicator_hidden=False, flash_showing=True)
        assert events == ["hide", "dwm flush", "capture", "restore"]

    def test_no_flush_when_nothing_was_hidden(self, monkeypatch):
        events = self._desktop(monkeypatch, indicator_hidden=False, flash_showing=False)
        assert events == ["hide", "capture", "restore"]


class TestSuspendReportsVisibility:
    class _Indicator:
        def __init__(self, active):
            self.condition = threading.Condition()
            self.active = active
            self.suspended = 0
            self.changes = []

        def change(self, **kwargs):
            self.changes.append(kwargs)

    @pytest.mark.parametrize("active", [True, False])
    def test_yields_whether_indicator_was_showing(self, monkeypatch, active):
        indicator = self._Indicator(active)
        monkeypatch.setattr(control_overlay, "_instance", indicator)
        with control_overlay.suspend_for_capture() as hidden:
            assert hidden is active
        assert indicator.changes == [{"suspended": 1}, {"suspended": -1}]

    def test_yields_false_without_indicator(self, monkeypatch):
        monkeypatch.setattr(control_overlay, "_instance", None)
        with control_overlay.suspend_for_capture() as hidden:
            assert hidden is False


class TestCachedScrollState:
    @staticmethod
    def _node(values):
        from windows_mcp.uia import PropertyId

        node = MagicMock()
        node._is_cached = True
        node.GetCachedPropertyValue.side_effect = lambda pid: values.get(pid)
        node.GetCachedPattern.side_effect = AssertionError("no live pattern call")
        return node, PropertyId

    def test_reads_scroll_metadata_from_cache(self):
        from windows_mcp.tree.service import _cached_scroll_state
        from windows_mcp.uia import PropertyId

        node, _ = self._node(
            {
                PropertyId.ScrollVerticallyScrollableProperty: True,
                PropertyId.ScrollHorizontallyScrollableProperty: False,
                PropertyId.ScrollVerticalScrollPercentProperty: 42.123,
                PropertyId.ScrollHorizontalScrollPercentProperty: -1.0,
            }
        )
        assert _cached_scroll_state(node) == {
            "horizontal_scrollable": False,
            "horizontal_scroll_percent": 0,
            "vertical_scrollable": True,
            "vertical_scroll_percent": 42.12,
        }

    @pytest.mark.parametrize("value", [False, None, object(), 1])
    def test_only_a_real_true_counts_as_scrollable(self, value):
        from windows_mcp.tree.service import _cached_scroll_state
        from windows_mcp.uia import PropertyId

        node, _ = self._node({PropertyId.ScrollVerticallyScrollableProperty: value})
        assert _cached_scroll_state(node) is None

    def test_uncached_node_uses_live_pattern(self):
        from windows_mcp.tree.service import _cached_scroll_state

        node = MagicMock(spec=["GetCachedPattern"])
        node.GetCachedPattern.return_value = MagicMock(
            VerticallyScrollable=True,
            HorizontallyScrollable=True,
            VerticalScrollPercent=10.0,
            HorizontalScrollPercent=55.254,
        )
        assert _cached_scroll_state(node) == {
            "horizontal_scrollable": True,
            "horizontal_scroll_percent": 55.25,
            "vertical_scrollable": True,
            "vertical_scroll_percent": 10.0,
        }


class TestCachedChildControls:
    def test_uses_cached_control_type_without_provider_call(self):
        from windows_mcp.tree.cache_utils import _control_from_cached_element
        from windows_mcp.uia import ButtonControl, ControlType

        element = MagicMock()
        element.CachedControlType = ControlType.ButtonControl
        type(element).CurrentControlType = property(
            lambda self: (_ for _ in ()).throw(AssertionError("live call"))
        )
        control = _control_from_cached_element(element)
        assert isinstance(control, ButtonControl)

    def test_cache_request_skips_unused_properties(self):
        from windows_mcp.tree.cache_utils import CacheRequestFactory
        from windows_mcp.uia import PropertyId

        added = []

        class FakeRequest:
            def __init__(self, *args):
                pass

            def AddProperty(self, pid):
                added.append(pid)

            def AddPattern(self, pid):
                raise AssertionError("no patterns are cached")

        import windows_mcp.tree.cache_utils as cache_utils

        original = cache_utils.CacheRequest
        cache_utils.CacheRequest = FakeRequest
        try:
            CacheRequestFactory.create_tree_traversal_cache()
        finally:
            cache_utils.CacheRequest = original
        for unused in (
            PropertyId.ClickablePointProperty,
            PropertyId.FrameworkIdProperty,
            PropertyId.IsContentElementProperty,
            PropertyId.LegacyIAccessibleStateProperty,
            PropertyId.SelectionItemSelectionContainerProperty,
        ):
            assert unused not in added
        for used in (
            PropertyId.NameProperty,
            PropertyId.ControlTypeProperty,
            PropertyId.RuntimeIdProperty,
            PropertyId.LegacyIAccessibleRoleProperty,
            PropertyId.ScrollVerticallyScrollableProperty,
            PropertyId.ScrollVerticalScrollPercentProperty,
        ):
            assert used in added


class TestGlowCache:
    def test_glow_is_rendered_once_per_layout(self, monkeypatch):
        import windows_mcp.desktop.flash_overlay as flash_overlay

        flash_overlay._glow_cache.clear()
        renders = []
        monkeypatch.setattr(
            flash_overlay,
            "_render_glow_rgba",
            lambda w, h, rects, *, outward: renders.append((w, h, rects)) or object(),
        )
        monkeypatch.setattr(flash_overlay, "_premultiplied_bgra", lambda image, i: b"bgra")
        rects = ((0, 0, 100, 50),)
        assert flash_overlay._glow_bitmap(100, 50, rects) == b"bgra"
        assert flash_overlay._glow_bitmap(100, 50, rects) == b"bgra"
        assert len(renders) == 1
        flash_overlay._glow_bitmap(200, 50, ((0, 0, 200, 50),))
        assert len(renders) == 2
        flash_overlay._glow_cache.clear()

    def test_cache_is_bounded(self, monkeypatch):
        import windows_mcp.desktop.flash_overlay as flash_overlay

        flash_overlay._glow_cache.clear()
        monkeypatch.setattr(flash_overlay, "_render_glow_rgba", lambda *a, **k: object())
        monkeypatch.setattr(flash_overlay, "_premultiplied_bgra", lambda image, i: b"x")
        for w in range(1, 10):
            flash_overlay._glow_bitmap(w, 1, ((0, 0, w, 1),))
        assert len(flash_overlay._glow_cache) <= flash_overlay._GLOW_CACHE_SIZE
        flash_overlay._glow_cache.clear()
