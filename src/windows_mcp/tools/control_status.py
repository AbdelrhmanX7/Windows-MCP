"""Read-only MCP control ownership status tool."""

from mcp.types import ToolAnnotations

from windows_mcp.desktop.control import get_controller


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="ControlStatus",
        description="Get desktop control owner, enabled state, gate policy, configured idle timeout and remaining user idle wait in seconds, plus the server's elevation ('elevation': is_admin, integrity, UAC, and the elevated worker's state). Settings are read-only and changes require a server restart.",
        annotations=ToolAnnotations(
            title="ControlStatus",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def control_status_tool() -> dict:
        status = get_controller().status()
        try:
            from windows_mcp.elevation import elevation_status

            status["elevation"] = elevation_status()
        except Exception:  # noqa: BLE001 - status must never fail on a non-Windows host
            status["elevation"] = {"is_admin": False, "integrity": "unknown"}
        return status
