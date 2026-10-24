---
name: outlook
title: Microsoft Outlook
description: Microsoft Outlook email and calendar (classic and new Outlook); compose and send an email with subject and body, reply, forward, search mail and open the calendar.
version: 1
source: builtin
match:
  process: [outlook.exe, olk.exe]
  aumid: ["Microsoft.Office.OUTLOOK.EXE.*", "Microsoft.OutlookForWindows_*"]
  name: ["* - Outlook", Outlook]
  exe: [outlook.exe, olk.exe]
uri_schemes: [mailto, outlook]
---

# Microsoft Outlook

## What it's for

Email, calendar and contacts. Two different apps share the name: **classic Outlook**
(`outlook.exe`, part of Office) and **new Outlook** (`olk.exe`, a packaged WebView2 app that
replaces Mail and Calendar on Win11). Main window titles look like `Inbox - user@x.com - Outlook`
or `Mail - <name> - Outlook`; a compose window is `Untitled - Message (HTML)` (classic).

## Launch and deep links

- `App(mode="launch", name="Outlook")` (new Outlook may be listed as "Outlook (new)")
- Compose via `mailto:` (opens the user's default mail app, which may not be Outlook):
  `PowerShell(command="Start-Process 'mailto:anna@example.com?subject=Lunch%20tomorrow&body=Are%20you%20free%3F'")`
  Parameters: `subject`, `body`, `cc`, `bcc`; separate several recipients with `,` or `;` (client-dependent);
  URL-encode text (`%20` space, `%0D%0A` new line, `%26` &, `%3F` ?).
- Classic Outlook CLI (outlook.exe is on App Paths):
  - New message: `Start-Process outlook -ArgumentList '/c','ipm.note'`
  - New message to someone: `Start-Process outlook -ArgumentList '/c','ipm.note','/m','anna@example.com'`
  - New message with attachment: `Start-Process outlook -ArgumentList '/a','"C:\\path\\file.pdf"'`

## Keyboard shortcuts

Classic Outlook (new Outlook supports the same set when its keyboard setting is "Outlook"):

| Action | Shortcut |
|---|---|
| New email (from anywhere) | `Ctrl+Shift+M` |
| New item in current view | `Ctrl+N` |
| Send | `Alt+S` (also `Ctrl+Enter` once enabled) |
| Reply / Reply all / Forward | `Ctrl+R` / `Ctrl+Shift+R` / `Ctrl+F` |
| Search mail | `Ctrl+E` or `F3` |
| Mail / Calendar / People / Tasks | `Ctrl+1` / `Ctrl+2` / `Ctrl+3` / `Ctrl+4` |
| New appointment | `Ctrl+Shift+A` |
| Mark read / unread | `Ctrl+Q` / `Ctrl+U` |
| Delete item | `Delete` |
| Check spelling | `F7` |

## Recipes

1. **Send an email**
   1. `PowerShell(command="Start-Process 'mailto:bob@example.com?subject=Report&body=Hi%20Bob%2C%0D%0Aattached.'")`
   2. `WaitFor(condition="active_window", window_name="Message", timeout=15)` (new Outlook: the
      compose pane opens inside the main window)
   3. Review with `Screenshot()`, then `Shortcut(shortcut="alt+s")` to send. Confirm with the user first
      if the voice command was ambiguous.
2. **Reply to the selected email**: `Shortcut(shortcut="ctrl+r")`, `Type(text="...")`, `Shortcut(shortcut="alt+s")`.
3. **Search mail**: `Shortcut(shortcut="ctrl+e")`, `Type(text="from:anna invoice", press_enter=true)`.
4. **Open the calendar**: `Shortcut(shortcut="ctrl+2")`.

## UI quirks and accessibility notes

- Classic Outlook is Win32 with a ribbon (`Alt` KeyTips); the message list is a UIA table with
  rows named by sender/subject. New Outlook is WebView2: its tree is web-style and sparser, so
  prefer shortcuts and `mailto:`.
- In classic Outlook `Ctrl+F` is Forward, not Find; use `Ctrl+E` to search.
- First use of `Ctrl+Enter` asks whether it should send; `Alt+S` avoids that prompt.
- Classic Outlook may show a security prompt when another program sends mail through it.
