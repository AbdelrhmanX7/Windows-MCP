---
name: discord
title: Discord
description: Discord chat and voice app; jump to a server, channel or direct message, send a message, and mute or deafen in voice chat.
version: 1
source: builtin
match:
  process: [discord.exe]
  aumid: [com.squirrel.Discord.Discord]
  name: [Discord, "* - Discord"]
  exe: [discord.exe]
uri_schemes: [discord]
---

# Discord

## What it's for

Text chat, voice channels and calls with friends and communities. The desktop app is Electron.

## Launch and deep links

- `App(mode="launch", name="Discord")`
- The installer puts it under `%LOCALAPPDATA%\Discord`; the supported launcher is
  `PowerShell(command="Start-Process \"$env:LOCALAPPDATA\\Discord\\Update.exe\" -ArgumentList '--processStart','Discord.exe'")`.
- `discord:` URIs (ids come from "Copy Link" / Developer Mode "Copy ID"):
  - Open the app: `Start-Process 'discord://'`
  - Channel: `Start-Process 'discord://-/channels/<server_id>/<channel_id>'`
  - Direct messages home: `Start-Process 'discord://-/channels/@me'`

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| Quick switcher (find server, channel, DM, friend) | `Ctrl+K` |
| Toggle mute / deafen | `Ctrl+Shift+M` / `Ctrl+Shift+D` |
| Previous / next channel | `Alt+Up` / `Alt+Down` |
| Previous / next unread channel | `Alt+Shift+Up` / `Alt+Shift+Down` |
| Previous / next server | `Ctrl+Alt+Up` / `Ctrl+Alt+Down` |
| Mark channel read | `Esc` |
| Upload a file | `Ctrl+Shift+U` |
| Emoji picker | `Ctrl+E` |
| Show all keyboard shortcuts | `Ctrl+/` |

## Recipes

1. **Message a friend or channel by name**
   1. `App(mode="switch", name="Discord")` (or launch it)
   2. `Shortcut(shortcut="ctrl+k")`, `Type(text="anna", press_enter=true)` (Enter picks the top match)
   3. Focus is now in the message box: `Type(text="on my way", press_enter=true)`
2. **Open a specific channel**: `Start-Process 'discord://-/channels/<server_id>/<channel_id>'`.
3. **Mute / unmute in voice**: `Shortcut(shortcut="ctrl+shift+m")` (Discord window focused);
   deafen with `ctrl+shift+d`. Users can also set global keybinds in Discord settings.
4. **Check the latest messages**: `Screenshot()` is usually more useful than `Snapshot()` here.

## UI quirks and accessibility notes

- Electron app with a sparse, slow-to-populate UIA tree; prefer `Ctrl+K` and shortcuts.
- Several `Discord.exe` processes run per instance; closing the window minimizes to the tray
  by default (it keeps running).
- On launch it checks for updates first (a small "Checking for updates" window) before the main window.
- `Enter` sends; `Shift+Enter` adds a new line.
