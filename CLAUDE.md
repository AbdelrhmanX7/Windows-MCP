# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Windows-MCP is a Python MCP (Model Context Protocol) server that bridges AI LLM agents with the Windows OS, enabling direct desktop automation. It exposes 26 tools via FastMCP:

| Group | Tools |
|---|---|
| Capture | `Screenshot`, `Snapshot`, `Scrape`, `DisplayInventory` |
| Input | `Click` (also `target=` in words), `Locate`, `Type`, `Scroll`, `Move` (also drag-and-drop via `drag=True`), `Shortcut`, `MultiSelect`, `MultiEdit` |
| Timing | `Wait`, `WaitFor` |
| System | `App`, `PowerShell`, `FileSystem`, `Registry`, `Process`, `Clipboard`, `Notification`, `ControlStatus` |
| Apps | `Apps` (installed-app inventory: list/info/refresh), `AppSkill` (per-app skill markdown; `generate`/`regenerate`) |
| Orchestration | `Tasks` (plans of dependent tasks + live `task_event`s), `RunParallel` (concurrent background calls, ordered screen calls) |

Tool names are defined by the `name=` argument of each `@mcp.tool(...)` in `src/windows_mcp/tools/`; that directory is the source of truth. Note the shell tool is registered as `PowerShell`, not `Shell`. Any subset can be removed at startup with `serve --exclude-tools` (e.g. `windows-mcp serve --exclude-tools PowerShell,Registry`).

## Build & Development Commands

```bash
uv sync --extra dev        # Install runtime and development dependencies
uv run windows-mcp serve   # Run the MCP server
uv run --extra dev ruff format .  # Format code
uv run --extra dev ruff check .   # Lint code
uv run --extra dev ruff check --fix .  # Lint and auto-fix
uv run --extra dev pytest         # Run all tests
uv run --extra dev pytest tests/test_desktop_views.py  # Run a single test file
```

**Package manager**: UV (not pip). **Python**: 3.14+. **Build backend**: setuptools.

## Architecture

The codebase follows a layered service architecture under `src/windows_mcp/`:

**Entry point** — `__main__.py`: Builds the FastMCP server, parses CLI flags, and selects the transport. Tool registration is delegated to `tools.register_all()`; an async lifespan initializes the Desktop, WatchDog, and Analytics singletons, which tools resolve lazily through the `get_desktop` / `get_analytics` callables.

**Tools layer** — `tools/`: One module per tool group, each exposing `register(mcp, *, get_desktop, get_analytics)`. `tools/__init__.py` holds the module list and `register_all()`. Tool functions are thin — they normalize arguments and delegate to a service package. The `@with_analytics` decorator wraps each one for telemetry, making it the existing precedent for cross-cutting concerns at the tool boundary.

**Desktop service** — `desktop/service.py`: High-level orchestrator. Manages window operations (launch, resize, switch), screenshots, mouse/keyboard actions, and clipboard. Interfaces with Tree service for UI element discovery. `desktop/views.py` defines data models: `DesktopState`, `Window`, `Size`, `BoundingBox`, `Status`.

**Tree service** — `tree/service.py`: Captures the Windows accessibility tree from active and background windows. Identifies interactive elements and scrollable areas. Uses `ThreadPoolExecutor` for multi-threaded UI traversal. `tree/views.py` defines `TreeElementNode`, `ScrollElementNode`, `TreeState`. `tree/config.py` has control type configurations.

**UIAutomation wrapper** — `uia/`: Low-level abstraction over the Windows UIAutomation COM API via `comtypes`. `core.py` wraps the main automation object, `controls.py` has control-specific logic, `patterns.py` wraps UIAutomation patterns, `enums.py` has COM enumerations, `events.py` handles event subscriptions.

**Resolver** — `resolver/`: Turns a target described in words (`Click(target="Save button")`, `"File > Save As"`) into an element inside the server, so the planner needs no Snapshot round trip. `uia_source.py` reads the foreground (or named) window plus its open menus/owned popups with one cached `FindAllBuildCache` per window (raw COM element, `Cached*` properties only); `scoring.py` is pure ranking (exact > prefix > word > fuzzy over Name/LegacyIAccessible name/AutomationId/HelpText, control-type hints parsed from the text, ambiguity penalty, 1-based `nth` in reading order); `vision.py` is the opt-in tier 2 (Set-of-Mark JPEG of the window to a vision model, Anthropic via `requests`); `service.py` ties them together, applies the confidence threshold and runs `A > B` chains. Below the threshold nothing is clicked and the top 3 candidates are returned. Used by `Click`/`Type` (`target=`) and the read-only `Locate` tool in `tools/input.py`.

**Task orchestration** — `tasks/` (pure Python, no Windows imports): `store.py` holds plans (DAG validation, status state machine, `next` lane split, optional JSON persistence), `events.py` the event bus (seq/ts stamping, MCP log push keyed by connection, JSON-lines file, webhook worker thread, history ring for `Tasks(mode="events")`), `screen_lock.py` the process-wide FIFO screen lock, `parallel.py` the RunParallel executor, and `runtime.py` the singleton plus `OrchestrationMiddleware`. `tools/tasks.py` registers both tools and inserts that middleware outermost, so screen-touching calls (classified by `control_policy.is_screen_call`: DIRECT plus App switch/resize, unknown tools included) are serialized before the control gate runs. `Tasks` and `RunParallel` are in `control_policy.GATE_EXEMPT`: RunParallel dispatches each inner call through `mcp.call_tool`, so every inner call is gated, policy-checked, screen-locked and tracked on its own; gating the outer call would deadlock on the gate's non-reentrant lock.

**WatchDog** — `watchdog/service.py`: Runs in a separate STA thread that polls the focused element (focus is not subscribed to; see #332) and can subscribe to structure/property UIAutomation events. Notifies the Tree service of focus changes so the accessibility tree stays current.

**Virtual Desktop Manager** — `vdm/core.py`: Tracks which windows belong to which Windows virtual desktop (Win10/11).

**Domain services** — thin packages backing the system tools: `filesystem/` (read/write/copy/move/delete/list/search/info), `registry/` (get/set/delete/list, implemented via PowerShell cmdlets), `powershell/` (`PowerShellExecutor` plus environment resolution), `process/` (list/kill), `notifications/`. Registry, PowerShell, App and Notification run their commands in a persistent PowerShell session (`powershell/session.py`; falls back to one process per call when the session is busy, fails, or is disabled), so a warm call costs milliseconds instead of a ~1 s process start.

**Apps & skills** — `apps/`: installed-app inventory. `sources.py` reads Get-StartApps + AppsFolder + Get-AppxPackage in one PowerShell call and the uninstall keys / HKCR URL protocols with `winreg`; `service.py` merges them into `AppRecord`s (name, aliases, AUMID, exe, process names, publisher, version, URI schemes, sources) and caches them in memory and on disk with a TTL. `skills/`: markdown+frontmatter skills (`frontmatter.py` is a small YAML-subset parser, no PyYAML), `store.py` loads `skills/builtin/*.md` (package data) and the user folder with precedence builtin > user > generated, `generate.py` builds a `generated` skill from the Win32 menu bar, ribbon KeyTips and a bounded UIA walk. `tools/apps.py` exposes `Apps`/`AppSkill`, the `skill://` resources, and the one-line Snapshot hint (in-memory process/title lookup).

**Infrastructure** — `infrastructure/`: cross-cutting concerns. `analytics.py` (optional PostHog telemetry, disabled with `ANONYMIZED_TELEMETRY=false`; records tool names and errors only, never arguments or outputs), `auth.py` and `oauth.py` (bearer-token and OAuth middleware for HTTP transports), `security.py` (SSRF validation, IP allowlist middleware), `config.py` (server configuration). Note `windows_mcp/config.py` at the package root is unrelated — it only holds the `WINDOWS_MCP_DEBUG` helpers.

## Code Style

- Formatter/linter: **Ruff** (line length 100, double quotes)
- Naming: PEP 8 — `snake_case` functions/variables, `PascalCase` classes, `UPPER_CASE` constants
- Type hints required on function signatures
- Google-style docstrings for public functions/classes

## Key Design Details

- Screenshots are capped to 1920x1080 for token efficiency
- Mouse/keyboard input uses UIA (same coordinate space as BoundingRectangle; no DPI mismatch)
- Screenshot is the preferred fast visual-context tool; Snapshot is the heavier path for UI element ids and DOM extraction
- Browser detection (Chrome, Edge, Firefox) triggers special DOM extraction mode in Snapshot
- Fuzzy string matching (`thefuzz`) is used for element name matching
- UI element fetching has retry logic (`THREAD_MAX_RETRIES=3` in tree service)
- The server supports stdio, SSE, and streamable HTTP transports

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_SCREENSHOT_SCALE` | `1.0` | Scale factor for screenshots (range `0.1`–`1.0`). Lower on 1440p/4K to stay under Claude Desktop's 1 MB limit. Resolved in `tools/_snapshot_helpers.py`. |
| `WINDOWS_MCP_SCREENSHOT_BACKEND` | `auto` | Screenshot backend: `auto`, `dxcam`, `mss`, `pillow`. `auto` uses dxcam (BGRA frames, one cached camera per output, full output grabbed then cropped) when the capture fits a single output, mss/pillow otherwise; dxcam is retired after 3 consecutive failures or an import failure. dxcam is imported lazily and warmed in the background at server start. Resolved in `desktop/screenshot.py`. |
| `WINDOWS_MCP_IMAGE_FORMAT` | `jpeg` | Inline image encoding for Screenshot/Snapshot: `jpeg` (fastest, ~5 ms at 1080p), `webp` (smallest, ~50 ms) or `png` (lossless, ~80 ms). `jpg` is an alias; invalid values warn and use `jpeg`. The per-call `image_format` parameter overrides it. `save_path` files still use their own extension's format. Resolved in `tools/_snapshot_helpers.py`. |
| `WINDOWS_MCP_IMAGE_QUALITY` | `80` | JPEG/WebP quality (`1`–`100`, clamped; invalid uses the default). Annotated/grid JPEGs use 4:4:4 chroma so box labels stay sharp. Resolved in `tools/_snapshot_helpers.py`. |
| `WINDOWS_MCP_MAX_TREE_ELEMENTS` | `500` | Max UI elements a single Snapshot/WaitFor tree capture may collect before it stops descending and returns a truncated tree (with a note in the output). Bounds both traversal time and response size on huge flat lists/grids (e.g. an unfiltered inventory view with thousands of rows). Resolved in `tree/budget.py`. |
| `WINDOWS_MCP_WINDOW_TIME_BUDGET_SEC` | `10` | Wall-clock seconds one window's tree walk may take before it stops descending and keeps the elements captured so far; later windows are still walked. `0` disables; invalid or negative values use the default. Resolved in `tree/budget.py` (#383). |
| `WINDOWS_MCP_MAX_WINDOW_ELEMENTS` | _(no cap)_ | Most elements a single window may contribute to a capture, so one huge window cannot spend the whole `WINDOWS_MCP_MAX_TREE_ELEMENTS` budget. Unset, `0` or invalid means no per-window cap. Resolved in `tree/budget.py`. |
| `WINDOWS_MCP_EXCLUDE_PROCESSES` | _(none)_ | Comma-separated process basenames (case-insensitive, exact) whose windows are never entered by UI Automation tree traversal, e.g. `Code.exe,Cursor.exe`. Resolved in `tree/service.py`. |
| `WINDOWS_MCP_SKIP_HOST_PROCESSES` | `on` | Skip windows owned by the process chain that launched the server (the MCP host, e.g. VS Code). The walk stops after the first ancestor that owns a visible window, and never goes past `explorer.exe`/system processes. Set to `0`/`false`/`no`/`off` to traverse them. Resolved in `tree/service.py` (#383). |
| `WINDOWS_MCP_UIA_CONNECTION_TIMEOUT_MS` | `2000` | IUIAutomation2 connection timeout for a window's accessibility provider. Resolved in `uia/core.py`. |
| `WINDOWS_MCP_UIA_TRANSACTION_TIMEOUT_MS` | `5000` | IUIAutomation2 timeout for a single UI Automation call into a provider. Resolved in `uia/core.py`. |
| `WINDOWS_MCP_PROFILE_SNAPSHOT` | _(off)_ | Set to `1`/`true`/`yes`/`on` to log per-stage timing for Screenshot/Snapshot. Checked in `tools/_snapshot_helpers.py` and `desktop/service.py`. |
| `ANONYMIZED_TELEMETRY` | `true` | Set to `false` to disable PostHog telemetry. Checked in `__main__.py` and `infrastructure/analytics.py`. |
| `POSTHOG_API_KEY` | Project default | Override the PostHog project write key used for anonymous telemetry. Set to an empty string to skip PostHog client initialization. Checked in `infrastructure/analytics.py`. |
| `POSTHOG_HOST` | `https://us.i.posthog.com` | Override the PostHog host for anonymous telemetry, such as for a self-hosted PostHog deployment. Checked in `infrastructure/analytics.py`. |
| `WINDOWS_MCP_WATCHDOG` | _(off)_ | Set to `on`/`1`/`true`/`yes`/`enabled` to start the UIA focus WatchDog thread. Unset, or any other value, leaves it off. Opt-in because it only emits debug logging today; focus is polled rather than subscribed to, removing the native focus-callback path that crashed the server (#332). Resolved in `__main__.py`. |
| `WINDOWS_MCP_DEBUG` | `false` | Set to `1`/`true`/`yes`/`on` to enable debug mode. Checked in `config.py`. Also available as `--debug` CLI flag. |
| `WINDOWS_MCP_RESOLVER_MIN_CONFIDENCE` | `0.7` | `Click`/`Type` with `target=` act only at or above this confidence (0–1); below it they click nothing and return the top candidates. Resolved in `resolver/config.py`. |
| `WINDOWS_MCP_RESOLVER_VISION` | _(off)_ | Set to `anthropic` (or `on`) to enable the tier-2 vision fallback, which sends a downscaled JPEG of the target window to the provider when accessibility matching is weak. An API key alone never enables it. Resolved in `resolver/config.py`. |
| `WINDOWS_MCP_RESOLVER_MODEL` | `claude-haiku-5-5` | Vision model for tier 2. Key from `ANTHROPIC_API_KEY`. Resolved in `resolver/config.py`. |
| `WINDOWS_MCP_RESOLVER_VISION_TIMEOUT` | `3.0` | Seconds allowed for the tier-2 request; on timeout or error the resolver degrades to tier-1 results. Resolved in `resolver/config.py`. |
| `WINDOWS_MCP_APPS_TTL` | `21600` | Seconds the installed-app inventory cache (memory + platformdirs user cache `apps-inventory.json`) stays fresh. `Apps(mode="refresh")` forces a rebuild. Resolved in `apps/service.py`. |
| `WINDOWS_MCP_SKILLS_DIR` | platformdirs user data dir `skills/` | Folder for user and generated skills. Resolved in `skills/store.py`. |
| `WINDOWS_MCP_TASKS_FILE` | _(off)_ | Persist task plans. `1`/`true`/`default` = `tasks.json` under platformdirs `user_data_dir("windows-mcp")`; otherwise a path. Resolved in `tasks/runtime.py`. |
| `WINDOWS_MCP_TASK_EVENTS_FILE` | _(off)_ | JSON-lines file receiving every task/plan event. Resolved in `tasks/runtime.py`. |
| `WINDOWS_MCP_TASK_WEBHOOK` | _(off)_ | URL that receives every event as a JSON POST (background thread, ordered, drops when 1000 are queued). Resolved in `tasks/runtime.py`. |
| `WINDOWS_MCP_TASK_WEBHOOK_TOKEN` | _(none)_ | Bearer token for the webhook. Resolved in `tasks/runtime.py`. |
| `WINDOWS_MCP_TASK_WEBHOOK_TIMEOUT` | `2.0` | Seconds per webhook POST. Resolved in `tasks/runtime.py`. |
| `WINDOWS_MCP_PARALLEL_MAX` | `4` | Default RunParallel background concurrency (capped at 16). Resolved in `tasks/parallel.py`. |
| `WINDOWS_MCP_PARALLEL_TIMEOUT` | `60` | Default per-call timeout for RunParallel background calls (screen calls are never abandoned mid-input). Resolved in `tasks/parallel.py`. |
| `WINDOWS_MCP_SCREEN_LOCK_TIMEOUT` | `60` | Seconds a screen-touching call waits for the FIFO screen lock before a `SCREEN_BUSY` error. Resolved in `tasks/screen_lock.py`. |
| `WINDOWS_MCP_DISABLE_FLASH` | _(off)_ | Set to `1`/`true`/`yes`/`on` to suppress the orange-red glowing border that briefly appears after every screenshot. Resolved in `desktop/flash_overlay.py`. |
| `WINDOWS_MCP_INPUT_SETTLE_MS` | `30` | Milliseconds slept after each injected click/key chord/wheel tick (was a fixed 500 ms from python-uiautomation; `500` restores it, range `0`–`5000`). Resolved in `uia/settle.py` and read on every call. Same-spot clicks are still kept apart by the double-click time and `Type` polls for the clicked window to take the foreground (`desktop/input_timing.py`). |
| `WINDOWS_MCP_POWERSHELL_SESSION` | `on` | Set to `off`/`0`/`false`/`no` to start a fresh PowerShell process per command instead of using the persistent session. Session semantics (state reset per command, timeout/respawn, one-shot fallback) are documented in `powershell/session.py`; resolved in `powershell/service.py`. |

## Security Context

This server has **full system access** with no sandboxing. `PowerShell`, `FileSystem`, `Registry`, `Process`, and `App` can all perform irreversible operations, and there is no audit log or rollback. The recommended deployment target is a VM or Windows Sandbox. Use `serve --exclude-tools` to drop the tools a given deployment does not need.
