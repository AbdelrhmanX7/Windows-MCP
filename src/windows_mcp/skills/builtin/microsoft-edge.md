---
name: microsoft-edge
title: Microsoft Edge
description: Microsoft Edge web browser; open a URL or web search, new tab or window, InPrivate browsing, find on page, bookmarks, history and downloads.
version: 1
source: builtin
match:
  process: [msedge.exe]
  aumid: [MSEdge]
  name: ["* - Microsoft*Edge", Microsoft Edge]
  exe: [msedge.exe]
uri_schemes: [microsoft-edge]
---

# Microsoft Edge

## What it's for

Web browsing. Window titles are `<page title> - Microsoft Edge` (Edge puts a zero-width space
between "Microsoft" and "Edge", hence the `Microsoft*Edge` pattern). With profiles the title can
end in `- <profile> - Microsoft Edge`.

## Launch and deep links

- `App(mode="launch", name="Microsoft Edge")`
- Open a URL in Edge regardless of the default browser:
  `PowerShell(command="Start-Process 'microsoft-edge:https://example.com'")`
- CLI (msedge.exe is on App Paths, so `Start-Process msedge` works):
  - `Start-Process msedge -ArgumentList 'https://example.com'`
  - `Start-Process msedge -ArgumentList '--inprivate','https://example.com'`
  - `Start-Process msedge -ArgumentList '--new-window','https://example.com'`
- Open a URL in the default browser instead: `Start-Process 'https://example.com'`.

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| New tab / close tab | `Ctrl+T` / `Ctrl+W` |
| Reopen closed tab | `Ctrl+Shift+T` |
| New window / InPrivate window | `Ctrl+N` / `Ctrl+Shift+N` |
| Focus address bar | `Ctrl+L` or `Alt+D` or `F6` |
| Next / previous tab | `Ctrl+Tab` / `Ctrl+Shift+Tab` |
| Go to tab 1-8 / last tab | `Ctrl+1`..`Ctrl+8` / `Ctrl+9` |
| Back / Forward | `Alt+Left` / `Alt+Right` |
| Reload / hard reload | `F5` / `Ctrl+F5` |
| Find on page | `Ctrl+F` |
| Bookmark page / Favorites | `Ctrl+D` / `Ctrl+Shift+O` |
| History / Downloads | `Ctrl+H` / `Ctrl+J` |
| Zoom in / out / reset | `Ctrl+Plus` / `Ctrl+Minus` / `Ctrl+0` |
| Full screen | `F11` |
| Print | `Ctrl+P` |
| Developer tools | `F12` or `Ctrl+Shift+I` |

## Recipes

1. **Open a website**: `PowerShell(command="Start-Process 'microsoft-edge:https://www.bbc.com'")`,
   then `WaitFor(condition="active_window", window_name="Microsoft Edge")`.
2. **Web search**: `Shortcut(shortcut="ctrl+t")`, `Type(text="weather in Cairo", press_enter=true)`
   (the address bar is focused in a new tab), or open `https://www.bing.com/search?q=<urlencoded>` directly.
3. **InPrivate window**: `Start-Process msedge -ArgumentList '--inprivate'` or `Shortcut(shortcut="ctrl+shift+n")`.
4. **Navigate the current tab**: `Shortcut(shortcut="ctrl+l")`, `Type(text="github.com", press_enter=true)`.
5. **Read the current URL**: `Shortcut(shortcut="ctrl+l")`, `Shortcut(shortcut="ctrl+c")`, `Clipboard(mode="get")`,
   then `Shortcut(shortcut="esc")`.

## UI quirks and accessibility notes

- Chromium-based: browser chrome (tabs, address bar) is in UIA, but web content is only exposed
  when the accessibility tree is active and can be sparse or slow; prefer shortcuts and URLs over clicks.
- The address bar element is named "Address and search bar".
- First launch may show welcome / sign-in / "set as default" prompts; `Snapshot` and dismiss.
- Edge keeps background processes (startup boost); many `msedge.exe` processes is normal.
