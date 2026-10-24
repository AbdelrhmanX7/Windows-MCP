---
name: microsoft-teams
title: Microsoft Teams
description: Microsoft Teams chat and meetings app; start a chat with someone, send a message, join a meeting link, mute, toggle camera, share screen and leave a call.
version: 1
source: builtin
match:
  process: [ms-teams.exe, teams.exe]
  aumid: ["MSTeams_*", com.squirrel.Teams.Teams]
  name: ["* | Microsoft Teams", Microsoft Teams]
  exe: [ms-teams.exe, teams.exe]
uri_schemes: [msteams]
---

# Microsoft Teams

## What it's for

Work/school chat, calls and meetings. **New Teams** is `ms-teams.exe` (packaged, WebView2);
**classic Teams** is `Teams.exe` (Electron, retired). Window titles look like
`Chat | <name> | Microsoft Teams`. The free consumer "Teams (personal)" uses the same new app.

## Launch and deep links

- `App(mode="launch", name="Microsoft Teams")`
- Deep links: replace `https://teams.microsoft.com` with `msteams:` to open in the app instead of the browser.
  - Chat with people (optionally prefilled message):
    `Start-Process 'msteams:/l/chat/0/0?users=anna@contoso.com,bob@contoso.com&message=Hi%20there'`
    (`topicName=<name>` names a group chat).
  - Join a meeting: `Start-Process '<https://teams.microsoft.com/l/meetup-join/... link>'` and choose
    "Open Teams" / "Join on the Teams app" in the browser page.
- Opening a deep link fills the chat but does not send; the message still needs `Enter`.

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| Show keyboard shortcuts | `Ctrl+.` |
| Search / go to | `Ctrl+E` |
| New chat | `Ctrl+N` |
| Open app-bar item 1, 2, 3... (Activity, Chat, Teams...) | `Ctrl+1`, `Ctrl+2`, `Ctrl+3`... |
| Toggle mute | `Ctrl+Shift+M` |
| Toggle camera | `Ctrl+Shift+O` |
| Share screen | `Ctrl+Shift+E` |
| Raise / lower hand | `Ctrl+Shift+K` |
| Leave call | `Ctrl+Shift+H` |
| Accept audio / video call | `Ctrl+Shift+S` / `Ctrl+Shift+A` |
| Decline call | `Ctrl+Shift+D` |
| Expand compose box | `Ctrl+Shift+X` |

## Recipes

1. **Message someone**
   1. `PowerShell(command="Start-Process 'msteams:/l/chat/0/0?users=anna@contoso.com&message=Running%205%20min%20late'")`
   2. `WaitFor(condition="active_window", window_name="Microsoft Teams", timeout=15)`
   3. `Screenshot()` to confirm the right chat, then `Shortcut(shortcut="enter")` to send.
   Without an email address: `Shortcut(shortcut="ctrl+n")`, `Type(text="Anna")`, pick from the
   suggestion list via `Snapshot`, `Shortcut(shortcut="tab")` into the message box, type, `Enter`.
2. **Join a meeting from a link**: `Start-Process '<https meeting link>'`, pick the Teams app on the
   browser page, then in the pre-join
   screen `Snapshot()` and `Click` "Join now" (set mic/camera toggles there first).
3. **In a call**: mute `Shortcut(shortcut="ctrl+shift+m")`, camera `ctrl+shift+o`, leave `ctrl+shift+h`
   (the meeting window must be focused: `App(mode="switch", name="Microsoft Teams")`).

## UI quirks and accessibility notes

- Both versions are web-based (WebView2 / Electron): the UIA tree is large and lazily populated;
  prefer deep links and shortcuts.
- The meeting runs in its own window (title starts with the meeting name or "Meeting"); shortcuts go
  to the focused Teams window.
- The `Ctrl+1`.. order follows the left app bar, which users can reorder.
- Accounts/tenants: deep links open in the currently signed-in account.
