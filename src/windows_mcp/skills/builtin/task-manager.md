---
name: task-manager
title: Task Manager
description: Windows Task Manager; see which apps and processes use CPU, memory and disk, end a frozen or unwanted task, and manage startup apps.
version: 1
source: builtin
match:
  process: [taskmgr.exe]
  aumid: ["*\\taskmgr.exe"]
  name: [Task Manager]
  exe: [taskmgr.exe]
uri_schemes: []
---

# Task Manager

## What it's for

Inspecting running processes and resource use, ending unresponsive apps, and toggling startup
apps. For voice asks like "close Chrome" or "what's using my memory", the `Process` tool answers in
one call; open Task Manager only when the user wants to see it.

## Launch and deep links

- `Shortcut(shortcut="ctrl+shift+esc")` (opens directly) or `App(mode="launch", name="Task Manager")`
- `PowerShell(command="Start-Process taskmgr")`
- Startup apps without Task Manager: `Start-Process 'ms-settings:startupapps'`.

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| Open Task Manager | `Ctrl+Shift+Esc` |
| Security screen (then choose Task Manager) | `Ctrl+Alt+Delete` (cannot be sent by automation) |
| Move through the process list | `Up` / `Down` |
| Expand / collapse a process group | `Right` / `Left` |

## Recipes

1. **End an app** (preferred, no UI):
   1. `Process(mode="list", name="chrome")` to confirm it is running and see PIDs.
   2. `Process(mode="kill", name="chrome.exe")`; add `force=true` only if a normal kill fails.
   Ending a process can lose unsaved work; confirm with the user when the app may have open documents.
2. **What is using the most memory / CPU**: `Process(mode="list", sort_by="memory", limit=10)`
   (or `sort_by="cpu"`).
3. **End a task in the UI**: `Shortcut(shortcut="ctrl+shift+esc")`,
   `WaitFor(condition="active_window", window_name="Task Manager")`, `Snapshot()`, select the app's row,
   then `Click` "End task" (Win11: the command bar at the top; Win10: the button at the bottom right).
4. **Disable a startup app**: `Start-Process 'ms-settings:startupapps'`, `Snapshot()`, `Click` the app's toggle;
   or in Task Manager open the "Startup apps" page (Win11 left nav) / "Startup" tab (Win10).
5. **Restart Windows Explorer** (taskbar frozen): `PowerShell(command="Stop-Process -Name explorer -Force")`;
   Windows restarts the shell automatically (if not, `Start-Process explorer`).

## UI quirks and accessibility notes

- Win10 Task Manager may open in "Fewer details" mode; click "More details" to see the tabs.
- Win11 22H2+ redesigned it: pages are in a left navigation pane (Processes, Performance,
  App history, Startup apps, Users, Details, Services) and actions are in a top command bar.
- The process list is a UIA tree/grid with rows named by app; values update every second, so a
  `Snapshot` can be large. Prefer the `Process` tool for reading data.
- Task Manager runs elevated when the user is an administrator (UAC prompt if needed); ending
  system or other users' processes requires elevation.
