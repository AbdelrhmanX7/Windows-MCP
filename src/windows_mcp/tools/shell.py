"""PowerShell tool — shell/command execution."""

from mcp.types import ToolAnnotations
from windows_mcp.desktop.utils import as_bool as _as_bool
from windows_mcp.infrastructure import with_analytics
from windows_mcp.powershell import PowerShellExecutor
from fastmcp import Context


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="PowerShell",
        description="Shell/command execution. Keywords: shell, run, execute, cmd, terminal, command line, script, admin, administrator, elevated, UAC. A comprehensive system tool for executing any PowerShell commands. Use it to navigate the file system, manage files and processes, and execute system-level operations. Capable of accessing web content (e.g., via Invoke-WebRequest), interacting with network resources, and performing complex administrative tasks. This tool provides full access to the underlying operating system capabilities, making it the primary interface for system automation, scripting, and deep system interaction. Set elevated=true to run the command with administrator rights (install software, manage services, write HKLM, firewall, scheduled tasks, Program Files); it runs through a persistent elevated worker — one UAC consent per server session when the no-prompt task is not installed, and a no-op when the server is already elevated. Under relaxed control policy this tool is not desktop-gated. Prefer background execution; coordinate with the user before commands known to interfere with their desktop, reusing existing authorization.",
        annotations=ToolAnnotations(
            title="PowerShell",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @with_analytics(get_analytics(), "Powershell-Tool")
    def powershell_tool(
        command: str, timeout: int = 30, elevated: bool | str = False, ctx: Context = None
    ) -> str:
        try:
            response, status_code = PowerShellExecutor.execute_command(
                command, timeout, elevated=_as_bool(elevated, "elevated")
            )
            return f"Response: {response}\nStatus Code: {status_code}"
        except Exception as e:
            raise
