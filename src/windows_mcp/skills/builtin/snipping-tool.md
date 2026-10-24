---
name: snipping-tool
title: Snipping Tool
description: Windows screenshot tools (Snipping Tool, Snip & Sketch and the screen-clip overlay); capture a region, window or the full screen, copy it to the clipboard, annotate and save it.
version: 1
source: builtin
match:
  process: [snippingtool.exe, screensketch.exe, screenclippinghost.exe]
  aumid: ["Microsoft.ScreenSketch_*", "*\\snippingtool.exe"]
  name: [Snipping Tool, Snip & Sketch]
  exe: [snippingtool.exe, screensketch.exe]
uri_schemes: [ms-screenclip, ms-screensketch]
---

# Snipping Tool

## What it's for

Interactive screenshots the user frames themselves, plus annotation. Versions:
- **Win11**: one packaged Snipping Tool (`Microsoft.ScreenSketch_*`, `SnippingTool.exe`), also used
  by `Win+Shift+S`; it can record video in newer builds.
- **Win10**: the classic Win32 Snipping Tool (`C:\Windows\System32\SnippingTool.exe`) **and** the
  packaged Snip & Sketch (`ScreenSketch.exe`).
- The capture overlay itself runs as `ScreenClippingHost.exe`.

**If you need a screenshot for yourself, use the `Screenshot` tool; this skill is for when the user
wants a capture.**

## Launch and deep links

- Region/window/full-screen overlay: `Shortcut(shortcut="win+shift+s")` or
  `PowerShell(command="Start-Process 'ms-screenclip:'")`. The result goes to the clipboard
  (and a notification lets the user open it for editing).
- Open the editor app: `App(mode="launch", name="Snipping Tool")` or `Start-Process 'ms-screensketch:'`
  (Snip & Sketch on Win10, Snipping Tool on Win11).

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| Screen-clip overlay (region, window, full screen) | `Win+Shift+S` |
| Full screen to file in `Pictures\Screenshots` | `Win+PrintScreen` |
| Full screen to clipboard | `PrintScreen` (Win11 may open the overlay instead, per Settings) |
| Active window to clipboard | `Alt+PrintScreen` |
| New snip (inside Snipping Tool) | `Ctrl+N` |
| Save snip / copy snip | `Ctrl+S` / `Ctrl+C` |
| Cancel the overlay | `Esc` |

## Recipes

1. **Let the user snip a region**: `Shortcut(shortcut="win+shift+s")`; the user drags the area,
   and the image lands on the clipboard. Do not try to drag the overlay yourself unless asked.
2. **Save a full-screen screenshot to a file**: `Shortcut(shortcut="win+printscreen")`; the file
   appears in `shell:Pictures\Screenshots` (`%USERPROFILE%\Pictures\Screenshots`) as `Screenshot (N).png`.
   Check with `FileSystem(mode="list", path="C:\\Users\\<you>\\Pictures\\Screenshots")`.
3. **Save the last clipboard snip** (Windows PowerShell 5.1):
   `Add-Type -AssemblyName System.Windows.Forms; (Get-Clipboard -Format Image).Save('C:\\Users\\<you>\\Pictures\\snip.png')`.
   Or paste it into Paint and save (see the Paint skill).
4. **Copy the active window only**: `Shortcut(shortcut="alt+printscreen")`.

## UI quirks and accessibility notes

- The overlay is full-screen and modal; while open, other input goes nowhere. `Esc` dismisses it.
- `Win+PrintScreen` briefly dims the screen and saves silently; no window opens.
- On Win11 the PrintScreen key opens the overlay when Settings > Accessibility > Keyboard
  "Use the Print screen key to open screen capture" is on (default on recent builds).
- Packaged Snip & Sketch / Win11 Snipping Tool windows are hosted like other packaged apps;
  match them by title.
