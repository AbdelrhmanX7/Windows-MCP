---
name: windows-terminal
title: Windows Terminal
description: Windows Terminal and the PowerShell and Command Prompt shells; open a terminal in a folder or with a profile, open tabs and split panes, and run a command for the user to see.
version: 1
source: builtin
match:
  process: [windowsterminal.exe, powershell.exe, pwsh.exe, cmd.exe]
  aumid: ["Microsoft.WindowsTerminal_*"]
  name: [Windows PowerShell, PowerShell, Command Prompt, "Administrator: *", "*\\cmd.exe", "*\\powershell.exe"]
  exe: [windowsterminal.exe, wt.exe, powershell.exe, pwsh.exe, cmd.exe]
uri_schemes: []
---

# Windows Terminal

## What it's for

A tabbed host for PowerShell, Command Prompt, WSL and other shells. Win11 22H2+ uses it as the
default terminal; on Win10 it is a Store install and shells may open in the classic console window
instead. The window title is the active tab's title (e.g. `Windows PowerShell`, `Command Prompt`,
`Administrator: Windows PowerShell`).

**If the user only needs a command's result, run it with the `PowerShell` tool instead**; open a
terminal only when they want to see or keep using it.

## Launch and deep links

- `App(mode="launch", name="Terminal")` (Start menu name; "Windows Terminal" on older builds)
- `wt` CLI (an app execution alias in `%LOCALAPPDATA%\Microsoft\WindowsApps`), via `PowerShell`:
  - Open in a folder: `Start-Process wt -ArgumentList '-d "C:\\Projects\\app"'`
  - Open a profile: `Start-Process wt -ArgumentList '-p "Command Prompt"'` (profile names as in settings,
    e.g. `"Windows PowerShell"`, `"PowerShell"`, `"Ubuntu"`)
  - New tab in the most recent window: `Start-Process wt -ArgumentList '-w 0 new-tab -d "C:\\logs"'` (`nt` = `new-tab`)
  - Split pane: `Start-Process wt -ArgumentList '-d C:\\a ; split-pane -V -d C:\\b'` (`sp`, `-V` / `-H`)
  - Run a command and keep the shell open:
    `Start-Process wt -ArgumentList '-d "C:\\Projects\\app" powershell -NoExit -Command "npm test"'`
  - As administrator: `Start-Process wt -Verb RunAs`
- Subcommands are separated by `;`. Passing the whole argument list as one quoted string (as above)
  keeps PowerShell from treating `;` as its own statement separator.

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| New tab (default profile) / new tab with profile N | `Ctrl+Shift+T` / `Ctrl+Shift+1`..`9` |
| New window | `Ctrl+Shift+N` |
| Duplicate tab | `Ctrl+Shift+D` |
| Close pane or tab | `Ctrl+Shift+W` |
| Next / previous tab / tab N | `Ctrl+Tab` / `Ctrl+Shift+Tab` / `Ctrl+Alt+1`..`9` |
| Split pane right / down / auto-duplicate | `Alt+Shift+Plus` / `Alt+Shift+Minus` / `Alt+Shift+D` |
| Move focus between panes | `Alt+Arrow` |
| Find | `Ctrl+Shift+F` |
| Command palette | `Ctrl+Shift+P` |
| Settings | `Ctrl+,` |
| Copy / Paste | `Ctrl+Shift+C` / `Ctrl+Shift+V` (`Ctrl+C` copies only when text is selected) |
| Zoom in / out / reset | `Ctrl+Plus` / `Ctrl+Minus` / `Ctrl+0` |
| Full screen | `F11` or `Alt+Enter` |
| Interrupt the running command | `Ctrl+C` (no selection) |

## Recipes

1. **Open a terminal in a folder**: `PowerShell(command="Start-Process wt -ArgumentList '-d \"C:\\Projects\\app\"'")`,
   then `WaitFor(condition="active_window", window_name="PowerShell")`.
2. **Run a command visibly in the open terminal**: `App(mode="switch", name="PowerShell")`,
   `Type(text="git status", press_enter=true)` (keyboard focus is already in the prompt).
3. **Get command output**: prefer `PowerShell(command="git -C C:\\Projects\\app status")`, which
   returns stdout directly; reading text back out of a terminal window is unreliable.
4. **Stop a running command**: focus the window, `Shortcut(shortcut="ctrl+c")`.
5. **Admin shell**: `Start-Process wt -Verb RunAs` (a UAC prompt appears on the secure desktop and
   cannot be clicked by automation; the user must approve it).

## UI quirks and accessibility notes

- The terminal buffer exposes UIA text, but it is large and `Snapshot` shows little structure;
  `Screenshot()` is better for checking what is on screen.
- Each tab's shell is a separate `powershell.exe` / `pwsh.exe` / `cmd.exe` child process; the window
  belongs to `WindowsTerminal.exe`. Classic console windows (Win10, or when Terminal is not default)
  are owned by the shell with `conhost.exe` hosting them.
- `Ctrl+V` pastes in Terminal and in PowerShell; multi-line pastes show a confirmation warning.
