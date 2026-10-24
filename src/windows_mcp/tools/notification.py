"""Notification tool — Windows toast notifications."""

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field
from windows_mcp.infrastructure import with_analytics
from fastmcp import Context
from windows_mcp import notifications


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Notification",
        description="Sends a Windows toast notification with a title and message. Optionally pass app_id (a registered AppUserModelID, e.g. 'Microsoft.Windows.Explorer') to show it under a specific app; by default it appears under Windows PowerShell. Avoid unnecessary notifications that interrupt the user; relaxed control policy does not gate this tool.",
        annotations=ToolAnnotations(
            title="Notification",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Notification-Tool")
    def notification_tool(
        title: Annotated[
            str,
            Field(description="The title/heading of the toast notification."),
        ],
        message: Annotated[
            str,
            Field(description="The body text of the toast notification displayed below the title."),
        ],
        app_id: Annotated[
            str | None,
            Field(
                description="Optional registered Application User Model ID to show the toast under (e.g. 'Microsoft.Windows.Explorer'). Defaults to Windows PowerShell's ID, which is always registered.",
            ),
        ] = None,
        ctx: Context = None,
    ) -> str:
        try:
            return notifications.send_notification(title, message, app_id)
        except Exception as e:
            raise
