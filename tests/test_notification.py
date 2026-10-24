"""Notification tool: the documented title+message call must work without an app_id (#494)."""

from unittest.mock import patch

from fastmcp import Client, FastMCP

from windows_mcp import notifications
from windows_mcp.notifications.service import DEFAULT_APP_ID
from windows_mcp.tools import notification as notification_tool_module


def _execute_ok(script, shell=None):
    return "", 0


def test_send_notification_defaults_to_registered_app_id():
    with patch(
        "windows_mcp.notifications.service.PowerShellExecutor.execute_command",
        side_effect=_execute_ok,
    ) as execute:
        result = notifications.send_notification("Title", "Body")

    script = execute.call_args.args[0]
    assert f"$appId = '{DEFAULT_APP_ID}'" in script
    assert execute.call_args.kwargs["shell"] == "powershell"
    assert result == 'Notification sent: "Title" - Body'


def test_send_notification_uses_explicit_app_id():
    with patch(
        "windows_mcp.notifications.service.PowerShellExecutor.execute_command",
        side_effect=_execute_ok,
    ) as execute:
        notifications.send_notification("Title", "Body", "Microsoft.Windows.Explorer")

    assert "$appId = 'Microsoft.Windows.Explorer'" in execute.call_args.args[0]


async def test_tool_accepts_documented_title_and_message_only():
    mcp = FastMCP("test")
    notification_tool_module.register(mcp, get_desktop=lambda: None, get_analytics=lambda: None)

    with patch(
        "windows_mcp.notifications.service.PowerShellExecutor.execute_command",
        side_effect=_execute_ok,
    ) as execute:
        async with Client(mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert "app_id" not in tools["Notification"].inputSchema.get("required", [])
            result = await client.call_tool(
                "Notification", {"title": "Hermes", "message": "Windows-MCP connected"}
            )

    assert not result.is_error
    assert f"$appId = '{DEFAULT_APP_ID}'" in execute.call_args.args[0]
