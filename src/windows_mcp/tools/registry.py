"""Registry tool — Windows Registry operations."""

from typing import Literal

from mcp.types import ToolAnnotations
from windows_mcp.desktop.utils import as_bool as _as_bool
from windows_mcp.infrastructure import with_analytics
from windows_mcp import registry
from windows_mcp.registry import RegistryType
from fastmcp import Context


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name='Registry',
        description='Read and write the Windows Registry. Keywords: regedit, registry key, HKEY, HKCU, HKLM, Windows settings, registry value, admin, elevated. Use mode="get" to read a value, mode="set" to create/update a value, mode="delete" to remove a value or key, mode="list" to list values and sub-keys under a path. Paths use PowerShell format (e.g. "HKCU:\\Software\\MyApp", "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion"). Set elevated=true for writes/deletes under HKLM or other admin-only keys; it runs through the elevated worker (one UAC consent per session, no-op when the server is already elevated).',
        annotations=ToolAnnotations(
            title="Registry",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Registry-Tool")
    def registry_tool(
        mode: Literal['get', 'set', 'delete', 'list'],
        path: str,
        name: str | None = None,
        value: str | None = None,
        type: RegistryType = 'String',
        elevated: bool | str = False,
        ctx: Context = None,
    ) -> str:
        try:
            elevated = _as_bool(elevated, 'elevated')
            if mode == 'get':
                if name is None:
                    return 'Error: name parameter is required for get mode.'
                return registry.get_value(path=path, name=name)
            elif mode == 'set':
                if name is None:
                    return 'Error: name parameter is required for set mode.'
                if value is None:
                    return 'Error: value parameter is required for set mode.'
                return registry.set_value(
                    path=path, name=name, value=value, reg_type=type, elevated=elevated
                )
            elif mode == 'delete':
                return registry.delete_entry(path=path, name=name, elevated=elevated)
            elif mode == 'list':
                return registry.list_key(path=path)
            else:
                return 'Error: mode must be "get", "set", "delete", or "list".'
        except Exception as e:
            raise
