---
name: microsoft-word
title: Microsoft Word
description: Microsoft Word word processor; create, open, edit, format and save documents, find and replace text, and export a document to PDF.
version: 1
source: builtin
match:
  process: [winword.exe]
  aumid: ["Microsoft.Office.WINWORD.EXE.*"]
  name: ["* - Word", Word]
  exe: [winword.exe]
uri_schemes: [ms-word]
---

# Microsoft Word

## What it's for

Writing and formatting documents (`.docx`). Window titles are `<document> - Word`
(`Document1 - Word` for a new unsaved document).

## Launch and deep links

- `App(mode="launch", name="Word")`
- `PowerShell(command="Start-Process winword")` (on App Paths); open a file:
  `Start-Process winword -ArgumentList '"C:\\path\\report.docx"'` or just `Start-Process 'C:\\path\\report.docx'`.
- `ms-word:` is for web documents: `Start-Process 'ms-word:ofe|u|https://contoso.sharepoint.com/.../doc.docx'`
  (`ofe` = open for edit, `ofv` = open for view). It does not take local paths.
- For unattended work, Word COM automation is faster than the UI (see recipe 3).

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| New document | `Ctrl+N` |
| Open / Save / Save As dialog | `Ctrl+O` / `Ctrl+S` / `F12` |
| Print | `Ctrl+P` |
| Close document | `Ctrl+W` |
| Undo / Redo | `Ctrl+Z` / `Ctrl+Y` |
| Bold / Italic / Underline | `Ctrl+B` / `Ctrl+I` / `Ctrl+U` |
| Align left / center / right / justify | `Ctrl+L` / `Ctrl+E` / `Ctrl+R` / `Ctrl+J` |
| Increase / decrease font size | `Ctrl+Shift+.` / `Ctrl+Shift+,` |
| Find (navigation pane) / Replace | `Ctrl+F` / `Ctrl+H` |
| Go to page / line | `Ctrl+G` |
| Insert hyperlink | `Ctrl+K` |
| Page break | `Ctrl+Enter` |
| Select all | `Ctrl+A` |
| Spelling & grammar | `F7` |
| Search commands ("Tell me") | `Alt+Q` |
| Ribbon KeyTips | `Alt` (then letters, e.g. `Alt`, `F` for File) |

## Recipes

1. **New document with text**: `App(mode="launch", name="Word")`, `WaitFor(condition="active_window", window_name="Word")`,
   on the start screen press `Shortcut(shortcut="ctrl+n")` (or `enter` on "Blank document"),
   then `Type(text="...")`.
2. **Save as PDF from the UI**: `Shortcut(shortcut="f12")`, in the Save As dialog set
   "Save as type" to PDF (`Snapshot` for the combo box), `Type` the file name, `Shortcut(shortcut="enter")`.
3. **Convert a .docx to PDF without UI** (one PowerShell call):
   `$w=New-Object -ComObject Word.Application; $d=$w.Documents.Open('C:\\in.docx'); $d.ExportAsFixedFormat('C:\\out.pdf',17); $d.Close(0); $w.Quit()`
   (17 = `wdExportFormatPDF`).
4. **Find and replace**: `Shortcut(shortcut="ctrl+h")`, `Type` into "Find what", `Shortcut(shortcut="tab")`,
   `Type` the replacement, then click "Replace All" (`Snapshot` for its label).
5. **Read document text**: `Shortcut(shortcut="ctrl+a")`, `Shortcut(shortcut="ctrl+c")`, `Clipboard(mode="get")`.

## UI quirks and accessibility notes

- Office ribbon: press `Alt` to show KeyTips, then the letters shown (e.g. `Alt, H` Home, `Alt, N` Insert).
  Shortcuts are faster and more reliable than clicking ribbon buttons.
- On launch Word shows a Start screen (template gallery), not a blank page, unless the user
  disabled it; `Ctrl+N` from there creates a blank document.
- `Ctrl+O` / `Ctrl+S` on an unsaved file open the Backstage view, not a dialog; `F12` always opens
  the classic Save As dialog.
- The document body is a `Document` element; large documents are not fully exposed in `Snapshot`.
- Files from the internet open in Protected View (read-only); click "Enable Editing" first.
