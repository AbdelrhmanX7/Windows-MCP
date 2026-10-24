---
name: microsoft-excel
title: Microsoft Excel
description: Microsoft Excel spreadsheet app; create and open workbooks, enter data and formulas, sum columns, sort and filter, go to a cell, and save or export to CSV or PDF.
version: 1
source: builtin
match:
  process: [excel.exe]
  aumid: ["Microsoft.Office.EXCEL.EXE.*"]
  name: ["* - Excel", Excel]
  exe: [excel.exe]
uri_schemes: [ms-excel]
---

# Microsoft Excel

## What it's for

Spreadsheets (`.xlsx`, `.csv`). Window titles are `<workbook> - Excel` (`Book1 - Excel` when new).

## Launch and deep links

- `App(mode="launch", name="Excel")`
- `PowerShell(command="Start-Process excel")` (on App Paths); open a file:
  `Start-Process 'C:\\path\\data.xlsx'`.
- `ms-excel:` is for web workbooks: `Start-Process 'ms-excel:ofe|u|https://.../book.xlsx'`
  (`ofe` = edit, `ofv` = view); not for local paths.
- To create a sheet from data, writing a `.csv` with `FileSystem(mode="write", ...)` and opening it
  is far faster than typing cell by cell.

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| New workbook | `Ctrl+N` |
| Open / Save / Save As dialog | `Ctrl+O` / `Ctrl+S` / `F12` |
| Edit active cell | `F2` |
| Go to cell (Name box dialog) | `Ctrl+G` or `F5` |
| Jump to A1 / to edge of data | `Ctrl+Home` / `Ctrl+Arrow` |
| Select column / row | `Ctrl+Space` / `Shift+Space` |
| AutoSum | `Alt+=` |
| Fill down / fill right | `Ctrl+D` / `Ctrl+R` |
| Insert today's date / current time | `Ctrl+;` / `Ctrl+Shift+;` |
| Format cells dialog | `Ctrl+1` |
| Toggle filter | `Ctrl+Shift+L` |
| Create table | `Ctrl+T` |
| Next / previous sheet | `Ctrl+PageDown` / `Ctrl+PageUp` |
| Insert new sheet | `Shift+F11` |
| Find / Replace | `Ctrl+F` / `Ctrl+H` |
| Undo / Redo | `Ctrl+Z` / `Ctrl+Y` |
| Enter same value in all selected cells | `Ctrl+Enter` |
| Search commands ("Tell me") | `Alt+Q` |
| Ribbon KeyTips | `Alt` |

## Recipes

1. **Put data in a new sheet**: `FileSystem(mode="write", path="C:\\Users\\<you>\\Documents\\data.csv", content="Name,Amount\nA,10\n")`,
   then `PowerShell(command="Start-Process 'C:\\Users\\<you>\\Documents\\data.csv'")`.
2. **Type into a specific cell**: `Shortcut(shortcut="ctrl+g")`, `Type(text="B5", press_enter=true)`,
   then `Type(text="=SUM(B1:B4)", press_enter=true)`.
3. **Sum a column**: select the cell below the numbers (via `Ctrl+G`), `Shortcut(shortcut="alt+equals")`,
   `Shortcut(shortcut="enter")`.
4. **Read cell values**: select the range via `Ctrl+G` (e.g. `A1:D20`), `Shortcut(shortcut="ctrl+c")`,
   `Clipboard(mode="get")` (tab-separated). Without UI: `Import-Csv` for CSVs, or COM
   (`$x=New-Object -ComObject Excel.Application; $b=$x.Workbooks.Open('C:\\b.xlsx'); $b.Sheets(1).Range('A1').Text; $b.Close($false); $x.Quit()`).
5. **Export to PDF without UI**: `$b.ExportAsFixedFormat(0,'C:\\out.pdf')` (0 = `xlTypePDF`) in the COM snippet above.

## UI quirks and accessibility notes

- The grid is a UIA `DataGrid`/`Table`; only visible cells are exposed, so do not try to read a whole
  sheet through `Snapshot`; use the clipboard or COM.
- Typing while a cell is selected replaces its content; `F2` edits in place. `Enter` moves down, `Tab` right.
- Excel opens to a Start screen; `Ctrl+N` or `Enter` on "Blank workbook" creates a sheet.
- Ribbon KeyTips: `Alt` then letters (`Alt, H` Home, `Alt, N` Insert, `Alt, A` Data).
- Files from the internet open in Protected View; click "Enable Editing" before typing.
