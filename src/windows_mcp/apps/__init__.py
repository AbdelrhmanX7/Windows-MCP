"""Installed-application inventory (Start menu, uninstall keys, AppX, URL protocols)."""

from windows_mcp.apps.models import AppRecord, normalize_name, slugify
from windows_mcp.apps.service import AppInventory, get_inventory, merge_sources

__all__ = [
    "AppInventory",
    "AppRecord",
    "get_inventory",
    "merge_sources",
    "normalize_name",
    "slugify",
]
