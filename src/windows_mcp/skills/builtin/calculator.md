---
name: calculator
title: Calculator
description: Windows Calculator app; standard, scientific, programmer and date calculations, unit and currency conversion, and reading the displayed result.
version: 1
source: builtin
match:
  process: [calculatorapp.exe, calculator.exe]
  aumid: ["Microsoft.WindowsCalculator_*"]
  name: [Calculator]
  exe: [calculatorapp.exe, calculator.exe, calc.exe]
uri_schemes: [calculator, ms-calculator]
---

# Calculator

## What it's for

Showing a calculation to the user, or using a specific mode (scientific, programmer, date
difference, unit converter). For a plain "what is 15% of 240" answer, computing it yourself or with
`PowerShell(command="15/100*240")` is faster than driving the app; open Calculator only if asked.

## Launch and deep links

- `App(mode="launch", name="Calculator")`
- `PowerShell(command="Start-Process calc")` (`calc.exe` is a stub that starts the packaged app)
- `PowerShell(command="Start-Process 'calculator:'")` (`ms-calculator:` also works)

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| Standard / Scientific mode | `Alt+1` / `Alt+2` |
| Graphing / Programmer / Date calculation | `Alt+3` / `Alt+4` / `Alt+5` |
| Equals | `Enter` |
| Clear (C) / Clear entry (CE) | `Esc` / `Delete` |
| Delete last digit | `Backspace` |
| Negate (+/-) | `F9` |
| Reciprocal (1/x) | `R` |
| Square root | `@` |
| Percent | `%` |
| Memory store / recall / add / subtract / clear | `Ctrl+M` / `Ctrl+R` / `Ctrl+P` / `Ctrl+Q` / `Ctrl+L` |
| Copy result / paste number | `Ctrl+C` / `Ctrl+V` |
| Show history | `Ctrl+H` |
| Always on top (compact overlay) on / off | `Alt+Up` / `Alt+Down` |

## Recipes

1. **Calculate something visibly**
   1. `PowerShell(command="Start-Process calc")`
   2. `WaitFor(condition="active_window", window_name="Calculator")`
   3. `Type(text="1234*56", press_enter=true)` (digits and `+ - * /` keys work directly)
   4. Read the result: `Snapshot()` and look for the element named `Display is <value>`,
      or `Shortcut(shortcut="ctrl+c")` then `Clipboard(mode="get")`.
2. **Scientific mode**: `Shortcut(shortcut="alt+2")`, then type the expression.
3. **Convert units or currency**: open the navigation menu (hamburger button, `Snapshot` for its
   label), choose the converter (e.g. "Currency", "Length"), then set the From/To combo boxes.
4. **Days between two dates**: `Shortcut(shortcut="alt+5")`, set the From/To date pickers via `Snapshot`.

## UI quirks and accessibility notes

- Packaged UWP app: the window frame belongs to `ApplicationFrameHost.exe`, the calculator logic to
  `CalculatorApp.exe` (Win11) or `Calculator.exe` (Win10). Match by the title `Calculator`.
- Buttons have clear UIA names ("One", "Plus", "Equals"), and the display element's name is
  `Display is 1,234` (localized, uses the system thousands separator).
- `Alt+3` (Graphing) is only available when graphing mode is enabled (it can be disabled by policy).
- Typing `=` also evaluates; `Esc` clears everything before a new calculation.
