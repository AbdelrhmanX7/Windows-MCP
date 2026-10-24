from windows_mcp.powershell import PowerShellExecutor
from windows_mcp.powershell.utils import ps_quote, ps_quote_for_xml

__all__ = [
    "DEFAULT_APP_ID",
    "send_notification",
]

#: AppUserModelID of Windows PowerShell 5.1. Its Start-menu shortcut ships with every
#: Windows 10/11 install, so the ID is always registered and toasts sent under it are shown.
DEFAULT_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def send_notification(title: str, message: str, app_id: str | None = None) -> str:
    """Send a Windows toast notification with a title and message.

    Args:
        title: The title of the notification.
        message: The message of the notification.
        app_id: Application User Model ID the toast is shown under. Must be registered
            (have a Start-menu shortcut). Defaults to ``DEFAULT_APP_ID`` (Windows PowerShell).

    Returns:
        A string indicating the result of the notification.

    Notes:
        Windows uses the App ID as the app identity for desktop toast notifications
        and silently drops toasts whose ID does not match a registered
        shortcut/AppUserModelID, hence the always-registered default.
    """
    app_id = app_id or DEFAULT_APP_ID
    safe_title = ps_quote_for_xml(title)
    safe_message = ps_quote_for_xml(message)
    safe_app_id = ps_quote(app_id)

    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
        f"$notifTitle = {safe_title}\n"
        f"$notifMessage = {safe_message}\n"
        f"$appId = {safe_app_id}\n"
        '$template = @"\n'
        "<toast>\n"
        "    <visual>\n"
        '        <binding template="ToastGeneric">\n'
        "            <text>$notifTitle</text>\n"
        "            <text>$notifMessage</text>\n"
        "        </binding>\n"
        "    </visual>\n"
        "</toast>\n"
        '"@\n'
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        "$xml.LoadXml($template)\n"
        "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)\n"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml\n"
        "$notifier.Show($toast)"
    )
    # Use Windows PowerShell (5.1) explicitly because the WinRT toast APIs are not available in PowerShell 7+ (pwsh).
    response, status = PowerShellExecutor.execute_command(ps_script, shell="powershell")
    if status == 0:
        return f'Notification sent: "{title}" - {message}'
    else:
        return f"Notification may have been sent. PowerShell output: {response[:200]}"
