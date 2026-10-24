"""Tests for the ``save_path`` option on the Screenshot and Snapshot tools (#496)."""

import asyncio
import os
from unittest.mock import MagicMock

import pytest
from PIL import Image

import windows_mcp.__main__ as main_module
from windows_mcp.desktop.views import DesktopState, Size
from windows_mcp.tools._snapshot_helpers import resolve_save_path
from windows_mcp.tree.views import TreeState


@pytest.fixture
def fake_desktop(monkeypatch):
    image = Image.new("RGB", (64, 48), "white")
    image.putpixel((3, 4), (255, 0, 0))
    state = DesktopState(
        active_desktop={"name": "Desktop 1"},
        all_desktops=[{"name": "Desktop 1"}],
        active_window=None,
        windows=[],
        screenshot=image,
        cursor_position=(1, 2),
        screenshot_original_size=Size(width=64, height=48),
        tree_state=TreeState(),
    )
    desktop = MagicMock()
    desktop.get_state.return_value = state
    monkeypatch.setattr(main_module, "desktop", desktop)
    return desktop


class TestResolveSavePath:
    def test_none_or_blank_means_no_save(self):
        assert resolve_save_path(None) is None
        assert resolve_save_path("  ") is None

    def test_rejects_relative_path(self):
        with pytest.raises(ValueError, match="absolute"):
            resolve_save_path("shot.png")

    def test_rejects_unsupported_extension(self, tmp_path):
        with pytest.raises(ValueError, match="unsupported extension"):
            resolve_save_path(str(tmp_path / "shot.gif"))
        with pytest.raises(ValueError, match="unsupported extension"):
            resolve_save_path(str(tmp_path / "no_extension"))

    @pytest.mark.parametrize(
        ("name", "image_format"),
        [("a.png", "PNG"), ("a.JPG", "JPEG"), ("a.jpeg", "JPEG"), ("a.bmp", "BMP")],
    )
    def test_extension_selects_format(self, tmp_path, name, image_format):
        path, fmt = resolve_save_path(str(tmp_path / name))
        assert fmt == image_format
        assert path == (tmp_path / name).resolve()

    def test_creates_missing_parent_directories(self, tmp_path):
        target = tmp_path / "nested" / "deeper" / "shot.png"
        path, _ = resolve_save_path(str(target))
        assert path.parent.is_dir()

    def test_existing_directory_gets_timestamped_png(self, tmp_path):
        path, fmt = resolve_save_path(str(tmp_path))
        assert fmt == "PNG"
        assert path.parent == tmp_path.resolve()
        assert path.name.startswith("screenshot-") and path.suffix == ".png"

    def test_trailing_separator_creates_directory(self, tmp_path):
        path, _ = resolve_save_path(str(tmp_path / "shots") + os.sep)
        assert path.parent == (tmp_path / "shots").resolve()
        assert path.parent.is_dir()


class TestScreenshotToolSavePath:
    def test_saves_png_and_reports_path(self, fake_desktop, tmp_path):
        target = tmp_path / "out" / "shot.png"

        result = asyncio.run(main_module.screenshot_tool(save_path=str(target)))

        assert len(result) == 2, "image must still be returned inline"
        assert target.is_file()
        assert f"Screenshot Saved To: {target.resolve()}" in result[0]
        with Image.open(target) as saved:
            assert saved.size == (64, 48)
            assert saved.getpixel((3, 4))[:3] == (255, 0, 0)

    def test_saves_jpeg(self, fake_desktop, tmp_path):
        target = tmp_path / "shot.jpg"

        result = asyncio.run(main_module.screenshot_tool(save_path=str(target)))

        with Image.open(target) as saved:
            assert saved.format == "JPEG"
        assert "Screenshot Saved To:" in result[0]

    def test_saves_into_directory(self, fake_desktop, tmp_path):
        result = asyncio.run(main_module.screenshot_tool(save_path=str(tmp_path)))

        files = list(tmp_path.glob("screenshot-*.png"))
        assert len(files) == 1
        assert f"Screenshot Saved To: {files[0].resolve()}" in result[0]

    def test_without_save_path_writes_nothing(self, fake_desktop, tmp_path):
        result = asyncio.run(main_module.screenshot_tool())

        assert "Screenshot Saved To:" not in result[0]
        assert list(tmp_path.iterdir()) == []

    def test_invalid_path_returns_error_without_capturing(self, fake_desktop):
        result = asyncio.run(main_module.screenshot_tool(save_path="relative.png"))

        assert len(result) == 1
        assert "absolute" in result[0]
        fake_desktop.get_state.assert_not_called()


class TestSnapshotToolSavePath:
    def test_saves_when_vision_enabled(self, fake_desktop, tmp_path):
        target = tmp_path / "snap.png"

        result = asyncio.run(main_module.state_tool(use_vision=True, save_path=str(target)))

        assert len(result) == 2
        assert target.is_file()
        assert f"Screenshot Saved To: {target.resolve()}" in result[0]

    def test_requires_use_vision(self, fake_desktop, tmp_path):
        target = tmp_path / "snap.png"

        result = asyncio.run(main_module.state_tool(save_path=str(target)))

        assert "save_path requires use_vision=True" in result[0]
        assert not target.exists()
        fake_desktop.get_state.assert_not_called()
