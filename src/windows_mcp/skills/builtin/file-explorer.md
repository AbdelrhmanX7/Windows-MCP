---
name: file-explorer
title: File Explorer
description: Windows file manager; open folders and known folders like Downloads, find, rename, copy, move and delete files, and reveal a file in its folder.
version: 1
source: builtin
match:
  process: [explorer.exe]
  aumid: [Microsoft.Windows.Explorer]
  name: ["* - File Explorer", File Explorer, This PC, Quick access, Home, Downloads, Documents, Desktop]
  exe: [explorer.exe]
uri_schemes: []
---

# File Explorer

## What it's for

Browsing and managing files and folders. Win11 titles look like `Downloads - File Explorer`;
Win10 titles are just the folder name (`Downloads`, `This PC`). Folder windows have class
`CabinetWClass`. The same `explorer.exe` process also runs the taskbar and desktop
(`Program Manager`, `Shell_TrayWnd`), so never kill it to close a window.

## Launch and deep links

- `Shortcut(shortcut="win+e")` or `App(mode="launch", name="File Explorer")`
- Open a folder: `PowerShell(command="Start-Process explorer.exe 'C:\\Users\\Public'")` (or `ii <path>`)
- Known folders via `shell:` names: `Start-Process explorer.exe 'shell:Downloads'`.
  Useful names: `shell:Downloads`, `shell:Desktop`, `shell:Personal` (Documents), `shell:My Pictures`,
  `shell:My Music`, `shell:My Video`, `shell:Recent`, `shell:Startup`, `shell:SendTo`,
  `shell:RecycleBinFolder`, `shell:AppsFolder` (all installed apps).
- Reveal a file selected in its folder: `Start-Process explorer.exe '/select,"C:\\path\\file.txt"'`
- Open a search: `Start-Process 'search-ms:query=report&crumb=location:C:\\Users\\me\\Documents'`

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| New window | `Ctrl+N` |
| New tab / close tab (Win11 22H2+) | `Ctrl+T` / `Ctrl+W` |
| Focus address bar | `Ctrl+L` or `Alt+D` |
| Focus search box | `Ctrl+E` or `Ctrl+F` |
| Back / Forward / Up one level | `Alt+Left` / `Alt+Right` / `Alt+Up` |
| New folder | `Ctrl+Shift+N` |
| Rename | `F2` |
| Properties | `Alt+Enter` |
| Delete (Recycle Bin) / delete permanently | `Delete` / `Shift+Delete` |
| Copy / Cut / Paste | `Ctrl+C` / `Ctrl+X` / `Ctrl+V` |
| Select all | `Ctrl+A` |
| Refresh | `F5` |
| Preview pane / Details pane | `Alt+P` / `Alt+Shift+P` |
| Details view / Large icons view | `Ctrl+Shift+6` / `Ctrl+Shift+2` |

## Recipes

1. **Open Downloads (or another known folder)**
   1. `PowerShell(command="Start-Process explorer.exe 'shell:Downloads'")`
   2. `WaitFor(condition="active_window", window_name="Downloads")`
2. **Go to a typed path in the current window**: `Shortcut(shortcut="ctrl+l")`,
   `Type(text="C:\\Projects", press_enter=true)`
3. **Find files by name**: prefer `PowerShell(command="Get-ChildItem $HOME -Recurse -Filter '*invoice*' -ErrorAction SilentlyContinue | Select -First 20 FullName")`;
   to show results in the UI, `Shortcut(shortcut="ctrl+e")`, `Type(text="invoice", press_enter=true)`.
4. **Show the user a file**: `Start-Process explorer.exe '/select,"<full path>"'`.
5. **Create, move, copy or delete files**: skip the UI and use `FileSystem` or `PowerShell`
   (`New-Item`, `Move-Item`, `Copy-Item`, `Remove-Item`); open the folder afterwards if asked.
6. **Rename the selected item**: `Shortcut(shortcut="f2")`, `Type(text="new name", press_enter=true)`.

## UI quirks and accessibility notes

- The file list is a UIA `List` (Items View) whose rows are `ListItem`s named after the files;
  `Snapshot` exposes them, but long folders are virtualized (only visible rows appear).
- Win11 has tabs, a simplified command bar and a "See more" (`...`) menu; Win10 has a ribbon
  (Home / Share / View) with `Alt` KeyTips.
- Deleting or overwriting may raise a confirmation or "Replace or Skip Files" dialog; `Snapshot` it.
- `Ctrl+Shift+N` in Explorer is "new folder" (it is not an incognito shortcut here).
