---
name: paint
title: Paint
description: Microsoft Paint image editor; open, view and save images, paste a screenshot, crop, resize or rotate a picture, and convert between PNG and JPEG.
version: 1
source: builtin
match:
  process: [mspaint.exe]
  aumid: ["Microsoft.Paint_*", "*\\mspaint.exe"]
  name: ["* - Paint", Paint]
  exe: [mspaint.exe]
uri_schemes: []
---

# Paint

## What it's for

Simple raster editing: paste and save screenshots, crop, resize, rotate, draw, add text, and save
as PNG/JPEG/BMP/GIF. Window titles are `<file> - Paint` (`Untitled - Paint` when new).
Win10 ships classic Paint with a ribbon; Win11 ships a redesigned packaged Paint
(`Microsoft.Paint_*`, still started by `mspaint.exe`) with a toolbar instead of a ribbon.
Paint 3D is a different app and is not covered here.

## Launch and deep links

- `App(mode="launch", name="Paint")`
- `PowerShell(command="Start-Process mspaint")`; open an image:
  `Start-Process mspaint -ArgumentList '"C:\\Users\\<you>\\Pictures\\photo.png"'`
- No URI scheme for Paint itself.

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| New / Open | `Ctrl+N` / `Ctrl+O` |
| Save / Save As | `Ctrl+S` / `F12` |
| Print | `Ctrl+P` |
| Paste (e.g. a screenshot) | `Ctrl+V` |
| Select all / Copy / Cut | `Ctrl+A` / `Ctrl+C` / `Ctrl+X` |
| Undo / Redo | `Ctrl+Z` / `Ctrl+Y` |
| Resize and skew | `Ctrl+W` |
| Image properties (canvas size, units) | `Ctrl+E` |
| Crop to selection | `Ctrl+Shift+X` |
| Zoom in / out | `Ctrl+PageUp` / `Ctrl+PageDown` |
| Rulers / gridlines | `Ctrl+R` / `Ctrl+G` |
| Full-screen view | `F11` |
| Delete selection | `Delete` |

## Recipes

1. **Open an image**: `PowerShell(command="Start-Process mspaint -ArgumentList '\"C:\\path\\pic.png\"'")`,
   then `WaitFor(condition="active_window", window_name="Paint")`.
2. **Save the clipboard screenshot as a file**: `App(mode="launch", name="Paint")`,
   `Shortcut(shortcut="ctrl+v")` (the canvas grows to fit), `Shortcut(shortcut="f12")`,
   `Type(text="C:\\Users\\<you>\\Pictures\\shot.png", press_enter=true)`.
   Without UI (Windows PowerShell 5.1 only):
   `Add-Type -AssemblyName System.Windows.Forms; (Get-Clipboard -Format Image).Save('C:\\shot.png')`.
3. **Resize an image**: open it, `Shortcut(shortcut="ctrl+w")`, `Snapshot()` to find the
   Percentage/Pixels options and the Horizontal/Vertical boxes, set them, `Shortcut(shortcut="enter")`, save.
4. **Convert PNG to JPEG**: open the PNG, `Shortcut(shortcut="f12")`, set "Save as type" to JPEG
   (`Snapshot` for the combo box), type the new name, `Enter`.

## UI quirks and accessibility notes

- The canvas is a single custom element with no UIA content; drawing needs coordinate `Click`/drag
  and `Screenshot()` to verify.
- Classic (Win10) Paint ribbon: `Alt` shows KeyTips (`Alt, H` Home, `Alt, V` View, `Alt, F` File menu).
- Win11 Paint has layers and background removal in newer builds; its toolbar buttons have UIA names.
- Saving PNG with transparency as JPEG flattens it; Paint may warn about losing transparency.
