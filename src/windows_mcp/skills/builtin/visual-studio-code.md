---
name: visual-studio-code
title: Visual Studio Code
description: Visual Studio Code code editor; open a folder or file at a line, compare two files, run commands from the command palette, search the project and open the integrated terminal.
version: 1
source: builtin
match:
  process: [code.exe]
  aumid: [Microsoft.VisualStudioCode]
  name: ["* - Visual Studio Code", Visual Studio Code]
  exe: [code.exe]
uri_schemes: [vscode]
---

# Visual Studio Code

## What it's for

Editing code and text projects. Window titles are `<file> - <folder> - Visual Studio Code`
(a leading `●` means unsaved changes).

## Launch and deep links

- `App(mode="launch", name="Visual Studio Code")`
- CLI `code` (on PATH after a normal install; use it via `PowerShell`):
  - Open a folder or file: `code "C:\\Projects\\app"` / `code "C:\\Projects\\app\\main.py"`
  - Open at a line (and column): `code -g "C:\\Projects\\app\\main.py:42"` (`:42:5` for column)
  - Diff two files: `code --diff a.txt b.txt` (short `-d`)
  - New window / reuse the last window: `code -n <path>` / `code -r <path>`
  - Add a folder to the open workspace: `code -a <folder>`
  - Extensions: `code --install-extension <publisher.name>`, `code --list-extensions`
- URI: `Start-Process 'vscode://file/C:/Projects/app/main.py:42:5'` opens a file at line/column.

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| Command palette | `Ctrl+Shift+P` (or `F1`) |
| Go to file (quick open) | `Ctrl+P` |
| Go to line | `Ctrl+G` |
| Go to symbol in file | `Ctrl+Shift+O` |
| Find / Replace in file | `Ctrl+F` / `Ctrl+H` |
| Search across files | `Ctrl+Shift+F` |
| Toggle terminal / new terminal | ``Ctrl+` `` / ``Ctrl+Shift+` `` |
| Toggle sidebar | `Ctrl+B` |
| Explorer / Source control / Extensions | `Ctrl+Shift+E` / `Ctrl+Shift+G` / `Ctrl+Shift+X` |
| Settings | `Ctrl+,` |
| Save / Save all | `Ctrl+S` / `Ctrl+K S` (chord) |
| Close editor / reopen closed editor | `Ctrl+W` / `Ctrl+Shift+T` |
| Split editor | `Ctrl+\` |
| Toggle line comment | `Ctrl+/` |
| Format document | `Shift+Alt+F` |
| Go to definition | `F12` |
| New window | `Ctrl+Shift+N` |
| Open folder | `Ctrl+K Ctrl+O` (chord) |

## Recipes

1. **Open a project**: `PowerShell(command="code 'C:\\Projects\\app'")`, then
   `WaitFor(condition="active_window", window_name="Visual Studio Code")`.
2. **Open a file at a line**: `PowerShell(command="code -g 'C:\\Projects\\app\\main.py:120'")`.
3. **Run any command**: `Shortcut(shortcut="ctrl+shift+p")`, `Type(text="Format Document", press_enter=true)`.
   Typing the command name in the palette is the most reliable way to reach any feature.
4. **Go to a file in the open folder**: `Shortcut(shortcut="ctrl+p")`, `Type(text="settings.json", press_enter=true)`.
5. **Open the terminal**: `Shortcut(shortcut="ctrl+backtick")`, or palette `View: Toggle Terminal`.
   To just run a command, the `PowerShell` tool is faster than typing into VS Code's terminal.
6. **Chords** (`Ctrl+K S`): send them as two calls, `Shortcut(shortcut="ctrl+k")` then `Shortcut(shortcut="s")`.

## UI quirks and accessibility notes

- Electron app: the UIA tree is large but sparse for the editor itself; the editor text is not
  reliably readable through `Snapshot`. Read files with `FileSystem(mode="read", ...)` instead.
- The MCP host may itself be VS Code; with `WINDOWS_MCP_SKIP_HOST_PROCESSES` on (default) its
  windows are skipped during `Snapshot`, so rely on shortcuts and the CLI.
- `code` launched from `PowerShell` returns immediately; the window appears a moment later.
- Keybindings can be customized by the user; the palette always works.
