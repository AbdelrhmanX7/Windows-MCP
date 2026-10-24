---
name: notepad
title: Notepad
description: Plain-text editor built into Windows; open, edit, find/replace and save .txt files.
version: 1
source: builtin
match:
  process: [notepad.exe]
  aumid: ["*\\notepad.exe", "Microsoft.WindowsNotepad_*"]
  name: [Notepad, "* - Notepad"]
  exe: [notepad.exe]
uri_schemes: []
---

# Notepad

## What it's for

Quick plain-text editing: jot notes, paste text, edit config or log files, save as `.txt`.
No formatting. The window title is `<file name> - Notepad` (`Untitled - Notepad` for a new file;
a leading `*` means unsaved changes).

## Launch and deep links

- `App(mode="launch", name="Notepad")`
- Open a file directly: `PowerShell` -> `Start-Process notepad.exe -ArgumentList '"C:\path\file.txt"'`
- CLI: `notepad.exe <file>` opens the file; `notepad.exe /p <file>` prints it.
- No URI scheme.

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| New | `Ctrl+N` |
| Open | `Ctrl+O` |
| Save | `Ctrl+S` |
| Save As | `Ctrl+Shift+S` |
| Print | `Ctrl+P` |
| Undo | `Ctrl+Z` |
| Cut / Copy / Paste | `Ctrl+X` / `Ctrl+C` / `Ctrl+V` |
| Select all | `Ctrl+A` |
| Find | `Ctrl+F` |
| Find next / previous | `F3` / `Shift+F3` |
| Replace | `Ctrl+H` |
| Go to line | `Ctrl+G` |
| Insert time/date | `F5` |
| Zoom in / out / reset | `Ctrl+Plus` / `Ctrl+Minus` / `Ctrl+0` |

## Recipes

1. **Write a note and save it**
   1. `App(mode="launch", name="Notepad")`
   2. `WaitFor(condition="active_window", window_name="Notepad")`
   3. `Type(text="...")` (focus is already in the text area; no `loc` needed after launch)
   4. `Shortcut(shortcut="ctrl+s")`, then in the Save dialog `Type(text="C:\\Users\\<you>\\Desktop\\note.txt", press_enter=true)`
2. **Faster for pure file writes**: skip the UI and use `FileSystem(mode="write", path=..., content=...)`,
   then `PowerShell` -> `Start-Process notepad.exe -ArgumentList '"<path>"'` if the user wants to see it.
3. **Find and replace**: `Shortcut(shortcut="ctrl+h")`, type into "Find what", `Shortcut(shortcut="tab")`,
   type the replacement, then click "Replace All" (`Snapshot` to get its label).
4. **Read what is open**: `Shortcut(shortcut="ctrl+a")`, `Shortcut(shortcut="ctrl+c")`, `Clipboard(mode="get")`.

## UI quirks and accessibility notes

- Win10 Notepad has a classic Win32 menu bar (File, Edit, Format, View, Help; `Alt` + underlined letter).
  Win11 Notepad is a packaged app with tabs: `Ctrl+N` opens a new tab and `Ctrl+W` closes it.
- The text area is a single `Edit`/`Document` element; its text is readable in `Snapshot`.
- Closing with unsaved changes shows a "Do you want to save changes" dialog (Save / Don't Save / Cancel).
