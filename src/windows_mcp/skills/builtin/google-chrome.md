---
name: google-chrome
title: Google Chrome
description: Google Chrome web browser; open a URL or Google search, new tab or window, incognito browsing, find on page, bookmarks, history and downloads.
version: 1
source: builtin
match:
  process: [chrome.exe]
  aumid: [Chrome]
  name: ["* - Google Chrome", Google Chrome]
  exe: [chrome.exe]
uri_schemes: []
---

# Google Chrome

## What it's for

Web browsing. Window titles are `<page title> - Google Chrome`; with several profiles the title
can be `<page title> - Google Chrome - <profile>`.

## Launch and deep links

- `App(mode="launch", name="Google Chrome")`
- chrome.exe is registered on App Paths, so `Start-Process chrome` works:
  - `PowerShell(command="Start-Process chrome -ArgumentList 'https://example.com'")`
  - Incognito: `Start-Process chrome -ArgumentList '--incognito','https://example.com'`
  - New window: `Start-Process chrome -ArgumentList '--new-window','https://example.com'`
  - Specific profile: `Start-Process chrome -ArgumentList '--profile-directory="Profile 1"'`
- Chrome has no custom URI scheme; `Start-Process 'https://...'` uses the default browser.

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| New tab / close tab | `Ctrl+T` / `Ctrl+W` |
| Reopen closed tab | `Ctrl+Shift+T` |
| New window / incognito window | `Ctrl+N` / `Ctrl+Shift+N` |
| Focus address bar | `Ctrl+L` or `Alt+D` or `F6` |
| Next / previous tab | `Ctrl+Tab` / `Ctrl+Shift+Tab` |
| Go to tab 1-8 / last tab | `Ctrl+1`..`Ctrl+8` / `Ctrl+9` |
| Back / Forward | `Alt+Left` / `Alt+Right` |
| Reload / hard reload | `F5` / `Ctrl+F5` |
| Find on page | `Ctrl+F` |
| Bookmark page / bookmarks manager | `Ctrl+D` / `Ctrl+Shift+O` |
| Show/hide bookmarks bar | `Ctrl+Shift+B` |
| History / Downloads | `Ctrl+H` / `Ctrl+J` |
| Zoom in / out / reset | `Ctrl+Plus` / `Ctrl+Minus` / `Ctrl+0` |
| Full screen | `F11` |
| Print | `Ctrl+P` |
| Developer tools | `F12` or `Ctrl+Shift+I` |
| Task manager (Chrome) | `Shift+Esc` |

## Recipes

1. **Open a website**: `PowerShell(command="Start-Process chrome -ArgumentList 'https://www.youtube.com'")`,
   then `WaitFor(condition="active_window", window_name="Google Chrome")`.
2. **Google search**: `Shortcut(shortcut="ctrl+t")`, `Type(text="best pizza near me", press_enter=true)`,
   or open `https://www.google.com/search?q=<urlencoded query>` directly.
3. **Incognito**: `Start-Process chrome -ArgumentList '--incognito'` or `Shortcut(shortcut="ctrl+shift+n")`.
4. **Navigate the current tab**: `Shortcut(shortcut="ctrl+l")`, `Type(text="gmail.com", press_enter=true)`.
5. **Read the current URL**: `Shortcut(shortcut="ctrl+l")`, `Shortcut(shortcut="ctrl+c")`, `Clipboard(mode="get")`,
   then `Shortcut(shortcut="esc")`.
6. **Play a YouTube search**: open `https://www.youtube.com/results?search_query=<urlencoded>`,
   `Snapshot()`, `Click` the first video title.

## UI quirks and accessibility notes

- Chromium exposes web content to UIA only once the accessibility tree is active; it can be sparse
  or lag behind the page. Prefer URLs and shortcuts over clicking page elements.
- The address bar element is named "Address and search bar".
- Many `chrome.exe` processes per window is normal; do not kill them individually.
- Profile picker or "Restore pages?" bubbles can appear on launch; `Snapshot` and dismiss.
