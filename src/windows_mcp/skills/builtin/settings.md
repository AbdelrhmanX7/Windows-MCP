---
name: settings
title: Windows Settings
description: Windows Settings app; jump straight to a settings page such as display, sound, Bluetooth, Wi-Fi, Windows Update, default apps, wallpaper, night light or privacy with an ms-settings URI.
version: 1
source: builtin
match:
  process: [systemsettings.exe]
  aumid: ["windows.immersivecontrolpanel_*"]
  name: [Settings]
  exe: [systemsettings.exe]
uri_schemes: [ms-settings]
---

# Windows Settings

## What it's for

Changing system settings: display, sound, Bluetooth, network, updates, apps, personalization,
privacy, power. Always deep-link to the page with an `ms-settings:` URI instead of navigating.

## Launch and deep links

- `Shortcut(shortcut="win+i")` opens the home page.
- `PowerShell(command="Start-Process 'ms-settings:display'")` opens a page directly.

| Page | URI |
|---|---|
| Display (brightness, scale, resolution) | `ms-settings:display` |
| Night light | `ms-settings:nightlight` |
| Sound | `ms-settings:sound` |
| Volume mixer (per-app volume) | `ms-settings:apps-volume` |
| Bluetooth & devices | `ms-settings:bluetooth` |
| Printers & scanners | `ms-settings:printers` |
| Mouse / Touchpad | `ms-settings:mousetouchpad` / `ms-settings:devices-touchpad` |
| Wi-Fi | `ms-settings:network-wifi` |
| Network status | `ms-settings:network-status` |
| Ethernet / VPN / Proxy | `ms-settings:network-ethernet` / `ms-settings:network-vpn` / `ms-settings:network-proxy` |
| Airplane mode | `ms-settings:network-airplanemode` |
| Windows Update / check for updates now | `ms-settings:windowsupdate` / `ms-settings:windowsupdate-action` |
| Installed apps (uninstall) | `ms-settings:appsfeatures` |
| Default apps | `ms-settings:defaultapps` |
| Startup apps | `ms-settings:startupapps` |
| Optional features | `ms-settings:optionalfeatures` |
| Personalization / Background | `ms-settings:personalization` / `ms-settings:personalization-background` |
| Colors (dark mode) | `ms-settings:personalization-colors` |
| Lock screen / Themes / Taskbar | `ms-settings:lockscreen` / `ms-settings:themes` / `ms-settings:taskbar` |
| Date & time | `ms-settings:dateandtime` |
| Language & region | `ms-settings:regionlanguage` |
| Notifications | `ms-settings:notifications` |
| Power & battery / Battery saver | `ms-settings:powersleep` / `ms-settings:batterysaver` |
| Storage | `ms-settings:storagesense` |
| Clipboard | `ms-settings:clipboard` |
| Multitasking | `ms-settings:multitasking` |
| About (PC name, specs) | `ms-settings:about` |
| Privacy: microphone / camera / location | `ms-settings:privacy-microphone` / `ms-settings:privacy-webcam` / `ms-settings:privacy-location` |
| Sign-in options / Your info / Email & accounts | `ms-settings:signinoptions` / `ms-settings:yourinfo` / `ms-settings:emailandaccounts` |
| Windows Security | `ms-settings:windowsdefender` |
| Recovery / Activation | `ms-settings:recovery` / `ms-settings:activation` |

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| Open Settings | `Win+I` |
| Quick Settings (Win11) / Action Center (Win10) | `Win+A` |
| Project / second screen | `Win+P` |
| Move between controls / toggle a switch | `Tab` / `Space` |

## Recipes

1. **Open a settings page**: `PowerShell(command="Start-Process 'ms-settings:bluetooth'")`,
   then `WaitFor(condition="active_window", window_name="Settings")`.
2. **Flip a toggle (Bluetooth, night light, airplane mode)**: open the page, `Snapshot()`,
   find the toggle by its name (e.g. "Bluetooth"), `Click(label=<id>)`; `Snapshot()` again to confirm.
3. **Uninstall an app**: `Start-Process 'ms-settings:appsfeatures'`, type the app name in the
   "Search apps" box, then use the item's `...` / Uninstall button (confirm the dialog).
4. **Check for updates**: `Start-Process 'ms-settings:windowsupdate-action'`.
5. **Read-only questions** (PC name, Windows version) are faster via `PowerShell`
   (`Get-ComputerInfo -Property OsName,OsVersion,CsName`) than via the UI.

## UI quirks and accessibility notes

- Settings is a packaged UWP app: the visible window frame belongs to `ApplicationFrameHost.exe`
  and the content to `SystemSettings.exe`, so match the window by title `Settings`.
- Controls are XAML with good UIA names (toggles are `ToggleSwitch`/`Button` with On/Off state).
- Win10 and Win11 layouts differ a lot (Win11 has a left nav pane and nested pages), but the
  URIs above work on both; some pages redirect to the nearest Win11 equivalent.
- Changes apply immediately; there is no Save button. Admin-only settings show a UAC prompt.
