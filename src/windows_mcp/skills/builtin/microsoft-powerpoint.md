---
name: microsoft-powerpoint
title: Microsoft PowerPoint
description: Microsoft PowerPoint presentation app; create and open slide decks, add slides, start or control a slideshow, and save or export a presentation to PDF.
version: 1
source: builtin
match:
  process: [powerpnt.exe]
  aumid: ["Microsoft.Office.POWERPNT.EXE.*"]
  name: ["* - PowerPoint", PowerPoint]
  exe: [powerpnt.exe]
uri_schemes: [ms-powerpoint]
---

# Microsoft PowerPoint

## What it's for

Building and presenting slide decks (`.pptx`). Window titles are `<deck> - PowerPoint`
(`Presentation1 - PowerPoint` when new). During a slideshow a separate full-screen window
`PowerPoint Slide Show - <deck>` is shown.

## Launch and deep links

- `App(mode="launch", name="PowerPoint")`
- `PowerShell(command="Start-Process powerpnt")` (on App Paths); open a deck:
  `Start-Process 'C:\\path\\deck.pptx'`.
- Start a deck directly as a slideshow: `Start-Process powerpnt -ArgumentList '/s','"C:\\path\\deck.pptx"'`.
- `ms-powerpoint:` is for web decks: `Start-Process 'ms-powerpoint:ofe|u|https://.../deck.pptx'`.

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| New presentation | `Ctrl+N` |
| Open / Save / Save As dialog | `Ctrl+O` / `Ctrl+S` / `F12` |
| New slide | `Ctrl+M` |
| Start slideshow from beginning / from current slide | `F5` / `Shift+F5` |
| Next slide (in show) | `Right`, `Space`, `N`, `Enter` or `PageDown` |
| Previous slide (in show) | `Left`, `P`, `Backspace` or `PageUp` |
| Go to slide N (in show) | type the number, then `Enter` |
| Black / white screen (in show) | `B` / `W` |
| End slideshow | `Esc` |
| Bold / Italic / Underline | `Ctrl+B` / `Ctrl+I` / `Ctrl+U` |
| Undo / Redo | `Ctrl+Z` / `Ctrl+Y` |
| Find / Replace | `Ctrl+F` / `Ctrl+H` |
| Search commands ("Tell me") | `Alt+Q` |
| Ribbon KeyTips | `Alt` |

## Recipes

1. **Present a deck**: `Start-Process powerpnt -ArgumentList '/s','"C:\\path\\deck.pptx"'`; then
   `Shortcut(shortcut="right")` / `Shortcut(shortcut="left")` for next / previous, `Shortcut(shortcut="esc")` to end.
2. **Present the open deck**: `App(mode="switch", name="PowerPoint")`, `Shortcut(shortcut="f5")`.
3. **New deck with a title slide**: launch, `Shortcut(shortcut="ctrl+n")`, `Snapshot()`, click the
   "Click to add title" placeholder, `Type(text="...")`; `Shortcut(shortcut="ctrl+m")` adds the next slide.
4. **Export to PDF without UI**:
   `$p=New-Object -ComObject PowerPoint.Application; $d=$p.Presentations.Open('C:\\in.pptx',$true,$false,$false); $d.SaveAs('C:\\out.pdf',32); $d.Close(); $p.Quit()`
   (32 = `ppSaveAsPDF`).

## UI quirks and accessibility notes

- Placeholders ("Click to add title", "Click to add text") are exposed by name in `Snapshot`;
  click one, then type. `Esc` leaves text editing.
- Slideshow keys only work when the slideshow window has focus; `App(mode="switch")` to it first.
- PowerPoint opens to a Start screen; `Ctrl+N` creates a blank deck.
- Ribbon KeyTips: `Alt` then letters (`Alt, H` Home, `Alt, N` Insert, `Alt, S` Slide Show).
