# Integrating Windows-MCP with a planner backend

This guide is for a backend that runs an LLM planner (for example Claude) and drives
Windows through this MCP server, with a separate front end that shows live progress. The
running example is the request *"write me a note for groceries and then play my favorite
songs on Spotify"*.

```
 front end  <--- events (webhook / JSONL / your relay) ---+
     |                                                    |
     v                                                    |
 backend (LLM planner) ---- MCP (stdio or HTTP) ----> windows-mcp on the Windows PC
                       <--- tool results + task_event log notifications
```

## 1. Connecting

### stdio (backend runs on the same Windows machine)

The backend spawns the server and talks JSON-RPC over its stdin/stdout:

```python
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

transport = StdioTransport(
    command="uv", args=["run", "windows-mcp", "serve"],        # or "uvx windows-mcp serve"
    env={"ANONYMIZED_TELEMETRY": "false", "WINDOWS_MCP_TASK_WEBHOOK": "https://..."},
)
async with Client(transport, timeout=120, log_handler=on_log, mode="legacy") as client:
    ...
```

stdio has one session for the life of the process, so pushed events always reach it.
Start the server inside the interactive user session. A process started from a service or
over SSH (session 0) has no desktop, so input and screenshots fail there.

### Streamable HTTP (backend runs elsewhere: cloud, a laptop, a container)

```shell
windows-mcp auth --transport streamable-http --host 0.0.0.0 --port 8000 --with-tls   # once: key + cert + config
windows-mcp serve --transport streamable-http --host 0.0.0.0 --port 8000 \
  --auth-key "$WINDOWS_MCP_AUTH_KEY" --ip-allowlist "203.0.113.0/24" \
  --ssl-certfile cert.pem --ssl-keyfile key.pem
```

Clients connect to `https://<host>:8000/mcp/` with `Authorization: Bearer <key>`.

| `serve` flag | Env var | Purpose |
|---|---|---|
| `--transport stdio\|sse\|streamable-http` | | Transport (default `stdio`). Use `streamable-http` for network access. |
| `--host`, `--port` | | Bind address (default `localhost:8000`). Non-loopback hosts require auth unless `--allow-insecure-remote`. |
| `--auth-key` | `WINDOWS_MCP_AUTH_KEY` | Bearer token required on every HTTP request. |
| `--oauth-client-id`, `--oauth-client-secret` | `WINDOWS_MCP_OAUTH_CLIENT_ID/SECRET` | OAuth 2.0 + PKCE instead of (or alongside) a static key. |
| `--ip-allowlist` | `WINDOWS_MCP_IP_ALLOWLIST` | Comma-separated client IPs/CIDRs. |
| `--ssl-certfile`, `--ssl-keyfile` | `WINDOWS_MCP_SSL_CERTFILE/KEYFILE` | Serve HTTPS. |
| `--cors-origins` | `WINDOWS_MCP_CORS_ORIGINS` | Allow a browser origin to call the server directly. Off by default. |
| `--stateless-http` | `WINDOWS_MCP_STATELESS_HTTP` | No `Mcp-Session-Id`, so clients survive restarts. **This disables pushed events**; use the webhook or `Tasks(mode="events")` instead. |
| `--tools`, `--exclude-tools` | `WINDOWS_MCP_TOOLS`, `WINDOWS_MCP_EXCLUDE_TOOLS` | Expose only some tools (e.g. `--exclude-tools Registry`). |
| `--control/--no-control` | `WINDOWS_MCP_CONTROL` | Desktop ownership gate plus a visible "AI is in control" indicator (default on). |
| `--control-policy relaxed\|balanced\|strict` | | Which tools wait for desktop ownership (default `relaxed`: only direct input). |
| `--control-idle-timeout` | | Seconds of user inactivity before AI control resumes after a user takeover (default 10). |

**Keep `--control` on with `--control-policy relaxed` (both defaults) if you want
parallelism.** With `--no-control` the gate serializes every tool call. With `balanced`,
PowerShell, Notification, App launch, Process kill and Clipboard set each take the gate's
desktop lock. With `strict`, every call does. Under those settings, the affected
`RunParallel` background calls run one at a time.

### Receiving pushed events

Task events are sent as MCP log notifications (`notifications/message`, level `notice`,
logger `windows-mcp.tasks`, `data` = the event object). On FastMCP 4 clients:

- With `mode="legacy"` (the handshake protocol era), events arrive at any time, including
  changes made between requests.
- On the 2026-07-28 protocol era, the spec only allows log messages on the stream of an
  in-flight request. Events emitted while a request runs (every `Tasks` call, and every
  per-call update inside a long `RunParallel`) ride that request's stream. The server
  sends them before the response. Events emitted
  between requests are not delivered. Use the webhook or poll `Tasks(mode="events")` if you
  need those.

The **webhook** is the simplest channel for a front end: set `WINDOWS_MCP_TASK_WEBHOOK` to
an HTTP endpoint (a Convex HTTP action, a Next.js route, ...). Every event is POSTed as
JSON, in order, from one background thread with a short timeout. A slow or offline backend
never blocks a tool call.

## 2. Tools

Tool names are case-sensitive. **Screen** tools drive the shared mouse, keyboard or focus.
They never interleave, and `RunParallel` runs them in order. **Background** tools are safe
to run concurrently.

| Tool | Lane | Purpose |
|---|---|---|
| `Tasks` | local | Plans of dependent tasks; status updates; `next` lanes; live `task_event`s. |
| `RunParallel` | both | Run several tool calls in one turn: background concurrently, screen calls in order. |
| `Do` | screen | Hand a small concrete goal to a fast model inside the server; it runs the tools itself and returns a timed transcript. See section 10. |
| `Route` | background | Calibrated choice over fixed options (route an utterance, verify a step) with Microsoft-Decision-1. See section 10. |
| `Screenshot` | background | Fast capture: windows, cursor, image. The cheapest way to look. |
| `Snapshot` | background | Full UI tree with element ids (labels); `use_dom=True` for browsers. Slower. |
| `Locate` | background | Read-only: find the element a phrase like "Save button" refers to, without clicking. |
| `Scrape` | background | Fetch a web page as markdown (SSRF-protected). |
| `Browser` | background | Drive web pages in Edge/Chrome over CDP: open, search, read, click, fill, type, press, eval, wait, screenshot, tabs. No screen input, so it runs next to screen tools. See section 11. |
| `DisplayInventory` | background | Monitors, work areas, DPI. |
| `Click` | screen | Click at `loc`, a Snapshot `label`, or `target="Save button"` / `"File > Save As"` (resolved in-server, no Snapshot needed). |
| `Type` | screen | Type into `loc`/`label`/`target="search box"`; `clear`, `press_enter`. |
| `Scroll`, `Move`, `Shortcut` | screen | Scroll, move or drag, key chords like `ctrl+s`. |
| `MultiSelect`, `MultiEdit` | screen | Batch clicks / batch field entry. |
| `Wait` | background* | Sleep N seconds. *Put it in `lane="screen"` when it sequences screen steps. |
| `WaitFor` | background | Poll until text, a window or an element appears (one call instead of a loop). |
| `App` | `launch`/`launch_executable`: background*; `switch`/`resize`: screen | Launch by Start Menu name or exact exe; focus or resize windows. *A launch steals focus: put it in `lane="screen"` when a later screen call depends on it. |
| `PowerShell` | background | Run a command. Use it for URIs (`Start-Process 'spotify:'`), COM and APIs. |
| `FileSystem` | background | Read, write, copy, move, delete, list, search, info. |
| `Registry` | background | Get, set, delete or list values. |
| `Process` | `list`: background, `kill`: background | List or kill processes. |
| `Clipboard` | background | Get or set the clipboard. |
| `Notification` | background | Show a toast. |
| `ControlStatus` | local | Who controls the desktop (AI or user) and the remaining idle wait. |
| `Context` | background | Where the user is: foreground window, browser URL/domain, focused field (`is_password`), email recipients, plus the scam-guard verdict. A few ms. |
| `CheckLink` | background | Local phishing/lookalike check of a URL, domain or email address (or a list); sub-millisecond. |
| `Apps` / `AppSkill` | background; `AppSkill(generate/regenerate=true)`: screen | Available. Per-app skills (URI schemes, CLI/COM recipes, known shortcuts) so the planner can do common app actions without UI exploration. |

`Click(target=...)`, `Type(target=...)` and `Locate` (the click resolver) are available on
`main`. Below the confidence threshold (`WINDOWS_MCP_RESOLVER_MIN_CONFIDENCE`, default 0.7)
they click nothing and return the top three candidates.

## 3. The Tasks flow

### Statuses and transitions

```
pending  -> running | done | failed | skipped | cancelled
running  -> pending | done | failed | cancelled        (running -> running = progress update)
failed   -> pending | running | done | skipped | cancelled   (retry, or resolve by hand)
done, skipped, cancelled: terminal
```

- A dependency is **satisfied** when it is `done` or `skipped`.
- A pending task whose dependency chain contains a `failed` or `cancelled` task is
  **blocked**. It is reported, not run.
- `next` returns ready tasks in two lanes. `screen` (tasks with `needs_screen: true`) must
  run one at a time in the listed order, and `screen_busy` names any screen task still
  running. `background` tasks can all run now, in parallel.
- Plan status is one of `pending`, `active`, `blocked`, `done`, `failed` or `cancelled`.
- Tasks that were `running` when the server died reload as `failed` with
  `error: "interrupted: ..."` (when `WINDOWS_MCP_TASKS_FILE` persistence is on). Retry
  them by setting them back to `pending` or `running`.

### Worked example: "write me a note for groceries and then play my favorite songs on Spotify"

**Turn 1: plan.** The planner decomposes the request and creates the plan in one call:

```json
{"tool": "Tasks", "arguments": {
  "mode": "plan", "title": "Grocery note + favorite songs",
  "tasks": [
    {"id": "note",  "title": "Save the grocery list", "app": "Notepad",
     "steps": "FileSystem write %USERPROFILE%\\Documents\\groceries.txt"},
    {"id": "show",  "title": "Open the note in Notepad", "app": "Notepad",
     "needs_screen": true, "depends_on": ["note"]},
    {"id": "music", "title": "Play favorite songs", "app": "Spotify",
     "steps": ["PowerShell Start-Process 'spotify:playlist:<liked-songs-id>'"]},
    {"id": "tell",  "title": "Tell the user", "depends_on": ["show", "music"]}
  ]}}
```

The response shows the plan and the first `next`:

```
plan p1 "Grocery note + favorite songs" [pending] 0/4 done
  ...
next for plan p1 [pending]
  screen (run one at a time, in order): -
  background (parallel): note, music
  waiting: show, tell
```

**Turn 2: run the ready work in one call.** Link each call to its task, so status updates
happen without extra turns:

```json
{"tool": "RunParallel", "arguments": {"calls": [
  {"tool": "FileSystem", "task_id": "note",
   "args": {"mode": "write", "path": "C:\\Users\\me\\Documents\\groceries.txt",
            "content": "Groceries\n- milk\n- eggs\n- bread\n"}},
  {"tool": "PowerShell", "task_id": "music",
   "args": {"command": "Start-Process 'spotify:playlist:37i9dQZF1EVHGWrwldPRtj'"}}
]}}
```

Both calls run concurrently. `note` and `music` go `pending -> running -> done`, and the
front end sees each change as it happens.

**Turn 3: `next`, then the screen step.** `Tasks(mode="next")` now lists `show` in the
screen lane. The planner runs it:

```json
{"tool": "RunParallel", "arguments": {"calls": [
  {"tool": "App", "lane": "screen", "task_id": "show",
   "args": {"mode": "launch_executable", "executable": "C:\\Windows\\notepad.exe",
            "args": ["C:\\Users\\me\\Documents\\groceries.txt"]}}
]}}
```

**Turn 4: finish.** Run `tell` as a `Notification` with a `task_id`, or set it by hand
with `Tasks(mode="update", task_id="tell", status="done", result="...")`. Several updates
fit in one call: `updates=[{task_id, status, progress, result, error}, ...]`.

The plan reaches `done` in four tool turns. One tool per turn would take eight or more.

### Measured on the test VM (Windows 10, 8 vCPU, stdio, `--control` on)

The interactive e2e (`~/vm/win10-wb/wmcp-tasks-e2e.sh`) uses Notepad and Calculator as
stand-ins for Spotify. It runs five background calls (FileSystem write, PowerShell date,
a 600 ms PowerShell "playlist lookup", Registry get, Process list) and a screen chain
(App launch Notepad, Wait 1 s, `Type(target="Text Editor")`) in **one** `RunParallel`:

| | wall time | MCP round trips |
|---|---|---|
| Same 8 calls one by one | 9.2 s / 10.7 s (two runs) | 8 |
| One `RunParallel` | 7.1 s / 6.5 s (sum of call latencies 13.2 s / 12.0 s, x1.87 / x1.83) | 1 |

The background work (up to 2.4 s) was fully hidden behind the 6.5 to 7 s screen chain, which is dominated by the App launch (3.5 to 5.3 s; it slows down while PowerShell processes start in parallel). The screen calls
never overlapped. The client received 27 events (1 `plan_event`, 26 `task_event`s), in `seq`
order, and the JSONL file had the same 27. The bigger saving in practice is the seven LLM
turns that were not needed (each costs one to several seconds of model latency).
Launching Calculator by Start Menu name took about 10 s on the VM. Prefer URIs and exact
executables (see the tips below).

## 4. Event schema

Every event carries `seq` and `ts`. `seq` is monotonic per server process, so use it to
order events and de-duplicate across channels. `ts` is Unix epoch milliseconds. The same
object goes to MCP clients, the JSONL file (`WINDOWS_MCP_TASK_EVENTS_FILE`) and the webhook
body (`WINDOWS_MCP_TASK_WEBHOOK`), and to `Tasks(mode="events", since=<seq>)`, which returns
the last 500.

### `task_event`: emitted when a task is created and on every status or progress change

```json
{
  "type": "task_event",
  "seq": 26,
  "ts": 1792806634741,
  "plan_id": "p1",
  "task_id": "tell",
  "title": "Tell the user",
  "status": "running",
  "prev_status": "pending",
  "progress": "running Notification",
  "result": null,
  "error": null,
  "needs_screen": false,
  "plan_status": "active"
}
```

| Field | Type | Notes |
|---|---|---|
| `type` | `"task_event"` | |
| `seq` | int | Monotonic, starts at 1 per server process. |
| `ts` | int | Unix epoch milliseconds. |
| `plan_id`, `task_id` | string | |
| `title` | string | |
| `status` | `pending`, `running`, `done`, `failed`, `skipped` or `cancelled` | The new status. |
| `prev_status` | string or null | `null` when the task was just created. Equals `status` for progress-only updates. |
| `progress` | string or null | Free text, for example `"running PowerShell"` or `"2/3 calls succeeded"`. |
| `result` | any JSON or null | From `update` or the linked `RunParallel` call's text output (a list when several calls share a task). |
| `error` | string or null | |
| `needs_screen` | bool | |
| `plan_status` | `pending`, `active`, `blocked`, `done`, `failed` or `cancelled` | Plan status after this change. |
| `result_truncated`, `error_truncated`, `progress_truncated` | bool, optional | Present when that field was cut to 2000 characters in the event. The full value (up to 8000 characters) is in `Tasks(mode="get")`. |

### `plan_event`: emitted on plan create, add, cancel and clear

```json
{"type": "plan_event", "seq": 1, "ts": 1792806617000, "event": "created",
 "plan_id": "p1", "title": "Grocery note + favorite songs", "plan_status": "pending",
 "plan": {"id": "p1", "title": "...", "status": "pending", "created_at": 0, "updated_at": 0,
          "tasks": [{"id": "note", "title": "...", "app": "Notepad", "steps": "...",
                     "needs_screen": false, "depends_on": [], "status": "pending",
                     "progress": null, "result": null, "error": null, "attempts": 0,
                     "created_at": 0, "updated_at": 0, "started_at": null,
                     "finished_at": null}]}}
```

`event` is one of `created`, `tasks_added`, `cancelled` (whole plan) or `cleared`. On
`created` and `tasks_added`, one `task_event` (`prev_status: null`) follows for each new
task. A front end can render from `plan_event.plan` and then apply `task_event`s by
`(plan_id, task_id)`.

For a Convex mirror, upsert a `tasks` row keyed on `plan_id + task_id`, and ignore an event
whose `seq` is lower than the stored one.

## 5. RunParallel details

- `calls=[{tool, args, task_id?, plan_id?, lane?}]`, up to 64. Results come back in input
  order: `{index, tool, lane, ok, status (ok, error, timeout or skipped), latency_ms,
  started_ms, result, error, task_id, plan_id}`. The summary adds `wall_ms`, `sum_ms` and
  `speedup`. Use `format="json"` for machine output.
- **Background lane:** concurrent, at most `max_concurrency` at a time (default
  `WINDOWS_MCP_PARALLEL_MAX`=4, capped at 16). Each call has a `timeout` (default
  `WINDOWS_MCP_PARALLEL_TIMEOUT`=60 s). A timed-out tool that is already running in a
  worker thread finishes on its own, and its result is discarded.
- **Screen lane:** the DIRECT tools plus App `switch`/`resize`, and any call with
  `lane="screen"`. These run in order, concurrently with the background lane, and each one
  holds the screen lock while it runs (a `lane="screen"` App launch or Wait too, so a
  concurrent request's Click cannot land between "launch Notepad" and "Type"). Screen calls are never timed out mid-input. If a screen call
  fails, the remaining screen calls are `skipped` (set `stop_screen_on_error=false` to keep
  going). `lane="background"` on a screen tool is ignored.
- **Safety is unchanged.** Every inner call is dispatched through FastMCP's own `call_tool`,
  so it passes the control gate, the policy, the screen lock, argument validation and
  analytics exactly like a direct request. RunParallel itself counts as the strictest
  category of its inner calls. During a user takeover, a batch that needs control is rejected
  before anything runs. `Tasks` is never gated.
- **Task linking:** a call with `task_id` sets its task to `running` when it starts. When
  the last call linked to that task ends, the task becomes `done` (with the result) or
  `failed` (with the errors). A task cancelled before its call starts makes the call
  `skipped`.

## 6. Screen lock

All screen-touching calls are serialized by one process-wide, FIFO-fair lock. This holds
across concurrent MCP requests and across `RunParallel` calls. A call waits at most
`WINDOWS_MCP_SCREEN_LOCK_TIMEOUT` seconds (default 60), then fails with:

```
SCREEN_BUSY: Click waited 60s for the screen lock (held by Type, 2 call(s) ahead in queue). ...
```

Background tools never take it. A cancelled request keeps the lock until its tool really
finishes, so keystrokes from two calls never mix.

## 7. Recommended planner system prompt (snippet)

```text
You control a Windows PC through the windows-mcp tools. Finish the user's request in as few
tool turns as possible.

1. Plan first. For any request with more than one step, call Tasks(mode="plan") once with
   every task: short titles, "app", "needs_screen": true only for steps that click or type
   in a window, and "depends_on" for real ordering constraints only.
2. Prefer direct routes over the UI: URI schemes and exact executables
   (PowerShell Start-Process 'spotify:...', 'ms-settings:...', 'mailto:...'), FileSystem for
   files, Registry and PowerShell for settings, Scrape for web pages, and app skills
   (Apps/AppSkill) when available. Click through the UI only when there is no API.
3. Run every ready task in one RunParallel call per turn. Put all background-lane tasks in it,
   plus the screen-lane tasks in order (use lane="screen" for an App launch or Wait that a
   later Click/Type depends on). Set task_id on each call so progress updates itself.
4. For a concrete desktop goal of a few UI steps in one app ("open Notepad and type ...",
   "open Calculator and compute 12*34", "turn on dark mode"), call Do(goal=...) once instead
   of driving each Click/Type yourself. Put everything the inner model needs in goal and
   context (exact text, file names). Do not use Do for one-step actions, for anything
   that needs your judgment between steps, or when a URI/PowerShell route does it in one call.
5. Anything inside a web page goes through Browser, not the screen tools: Browser(mode="search")
   or "open" (it returns the loaded url/title, no Wait needed), "read" for text and numbered
   elements, then "click"/"fill" by element number or words (fill text can be a list,
   submit=true presses Enter). Use Click/Type/Screenshot only for native apps and browser
   chrome that is not part of a page.
6. For UI steps, act by name: Click(target="Save button"), Type(target="search box",
   text=...), "File > Save As" paths. Use Locate to check a target without clicking. Use
   WaitFor instead of repeated screenshots. Take a Screenshot only when you are unsure what
   is on screen; use Snapshot only when you need element labels.
7. After each RunParallel, call Tasks(mode="next") only if the next step is not obvious.
   Retry a failed task once with a different route, then mark it failed with a clear error.
8. When the plan is done, reply to the user in one or two sentences.
9. Before opening a link or emailing an address you did not get from the user, run
   CheckLink on it. If a tool fails with SCAM_GUARD_CONFIRM_REQUIRED, stop, tell the user
   what was detected (domain, brand, reasons) and retry with confirmed=true only after they
   explicitly say to go ahead.

```

## 8. Latency tips

- **Batch.** Each MCP round trip through an LLM costs far more than any tool. One
  `RunParallel` with eight calls replaced eight turns in the e2e above.
- **PowerShell and Registry pay process startup** (about 1 to 1.8 s each on the VM). Run them
  in parallel, or combine several commands in one script with `;`.
- **Launch by URI or exact path.** `App(mode="launch", name=...)` resolves the Start Menu
  and took 3.5 s for Notepad and 10 s for Calculator on the VM.
  `App(mode="launch_executable")` or `PowerShell Start-Process '<uri>'` skips that.
- **Use `Screenshot`, not `Snapshot`, to look.** Snapshot walks the accessibility tree. Use
  `Click(target=...)` instead of Snapshot plus a label.
- **Use `WaitFor`, not polling,** when an app needs time to appear.
- **Keep `--control` on and the policy `relaxed`.** `--no-control` serializes every tool, and `balanced`/`strict` serialize more of them.
- **Use stateless HTTP only with the webhook.** Pushed events need a session.

## 9. Scam guard

The server knows which website or email recipient the user is on and flags lookalikes such as
`rnicorsoft.com` (imitating microsoft), `paypa1.com`, `microsoft.com.verify-login.xyz`,
`https://microsoft.com@evil.xyz` or an email to `support@rnicorsoft.com`. It refuses to type
into or send to them until the user confirms. Nothing new is needed on the backend: verdicts
arrive as `security_event`s on the same event bus as `task_event`s.

### Pieces

| Piece | What it does | Cost |
|---|---|---|
| Local detector (`guard/detector.py`) | Registrable domain (bundled Public Suffix List, private section included, so `evil.github.io` is not `github.io`), IDN/punycode, IP literals, `user@host` and `host\@brand` tricks, confusable skeleton (`rn`→`m`, `vv`→`w`, `cl`→`d`, `0`→`o`, `1`→`l`, Cyrillic/Greek homoglyphs), edit distance to ~250 brands' labels, brand in a subdomain or hyphenated label, embedded real domain, risky TLDs and hosting suffixes, hyphens, http, password field, tech-support-scam text. Official brand domains are fast-pathed as safe. No network. | VM: first check of a host ~40–110 µs median (p95 190 µs), cached ~10 µs; suffix list + brands load once in ~12 ms at startup |
| Watcher (`guard/watcher.py`) | Own UI Automation thread. Polls the foreground window/title (Win32) every 100 ms, the focused element every 300 ms and the address bar every 1.5 s, debounced; re-probes only on change. Reads page text only for pages that are not known-safe. | full probe 8–16 ms on a non-safe page (focus ~1 ms, URL 0.1–0.3 ms from the cached address-bar element, page text 6–8 ms, password-box search ~6.5 ms); ~2 ms on a safe page; event ~0.6–1.5 s after Edge launch |
| Second opinion (`guard/model.py`) | Microsoft-Decision-1 via OpenRouter, gray zone only (suspicious, or an unknown site with a password field or scam-like text). Runs in the background, cached per domain for 15 min, and can only raise severity. | ~300 ms warm, off the fast path |
| Gate (`tools/guard.py` `GuardGate`) | Middleware between the orchestration layer and the control gate. | refusal in ~1.8 ms round trip; one probe only when the foreground window/tab changed |

### Tools

`Context(refresh=false, include_text=false)` returns JSON:

```json
{"window": {"title": "Sign in to your account - Microsoft Edge", "process": "msedge.exe",
            "app": "browser", "product": "edge"},
 "url": "https://rnicorsoft.com/login", "domain": "rnicorsoft.com",
 "focused": {"name": "Password", "control_type": "Edit", "is_password": true,
             "address_bar": false},
 "recipients": [], "has_password_field": true, "page_text_chars": 412,
 "guard": {"verdict": "dangerous", "score": 100, "brand": "Microsoft",
           "reasons": ["'rnicorsoft' imitates 'microsoft' (look-alike characters)",
                       "asks for a password on a look-alike site"],
           "ms": 0.14, "domain": "rnicorsoft.com", "host": "rnicorsoft.com", "kind": "url",
           "official": false, "gray": false},
 "source": "browser", "cached": true, "probe_ms": 12.4, "ms": 0.3}
```

Measured on the Win10 VM with Edge: `cached: true` calls take 0.03–0.1 ms in the server
(about 2 ms MCP round trip); `refresh=true` 14–16 ms on a non-safe page. `cached: true` means the watcher's context was reused (same window and tab title, under 2 s
old). `app` is `browser`, `email` (Outlook classic `outlook.exe`, new Outlook `olk.exe`, or a
webmail compose with recipients) or `other`. Page text is never returned, only its length.

`CheckLink(target, second_opinion=false)` takes a URL, bare domain, email address,
`Name <addr>` or `mailto:` (or a list of up to 20) and returns the `guard` object above plus
`target`. With `second_opinion=true` and a key, gray-zone targets wait up to
`WINDOWS_MCP_GUARD_TIMEOUT` for the model. Without it, a background request is started so the
next check (or the watcher) gets the cached answer.

Verdicts: `safe` (official or well-known site), `unknown` (no signals; most of the web),
`suspicious` (score 35–69) and `dangerous` (score 70+). Edit distance alone ("one typo
away") is at most `suspicious`. `dangerous` needs look-alike characters or a second signal.

### Confirm before risky input

These calls are refused with a `ToolError` while the current context is `dangerous`, or
`suspicious` with a password field or an email recipient. "Password field" means the focused
element is a password box, the page shows one anywhere (`has_password_field`, checked on
pages that are not known-safe), or the call's `target` names a password/passcode/PIN/OTP
field. A lookalike page with a login form is therefore `dangerous` as soon as it loads,
before anything is typed.

- `Type`, `MultiEdit`
- `Click(target=...)` (clicks by coordinates or label are not gated)
- `Shortcut` for send/submit/paste keys: `enter`, `ctrl+enter`, `alt+s`, `ctrl+v`,
  `shift+insert`, `ctrl+shift+v`, `ctrl+shift+enter`

Leaving a flagged page is never blocked: `Type(target="Address and search bar", ...)` and
a `Shortcut` while the address bar has focus pass, and so do other shortcuts (`ctrl+l`,
`ctrl+t`, `ctrl+w`, `alt+left`).

The error text is JSON:

```json
{"code": "SCAM_GUARD_CONFIRM_REQUIRED", "verdict": "dangerous", "score": 100,
 "domain": "rnicorsoft.com", "brand": "Microsoft", "source": "browser", "recipient": null,
 "reasons": ["'rnicorsoft' imitates 'microsoft' (look-alike characters)"],
 "message": "Blocked: this would type into or send the page rnicorsoft.com, which looks dangerous imitating Microsoft. Tell the user what was detected and ask whether to continue. Only after they explicitly approve, retry the same call with confirmed=true."}
```

All four tools accept `confirmed` (`true` or `"true"` only). A confirmed call goes through and
emits a `security_event` with `event: "user_confirmed"` for the audit trail. Calls inside
`RunParallel` are gated one by one, like the control gate.

### `security_event`

Same `seq` space, MCP log notifications (logger `windows-mcp.tasks`), JSON-lines file and
webhook as `task_event`. `Tasks(mode="events")` returns them too.

```json
{"type": "security_event", "event": "verdict", "seq": 41, "ts": 1792806634741,
 "verdict": "dangerous", "score": 100, "domain": "rnicorsoft.com", "brand": "Microsoft",
 "reasons": ["'rnicorsoft' imitates 'microsoft' (look-alike characters)",
             "asks for a password on a look-alike site"],
 "source": "browser", "url": "https://rnicorsoft.com/login", "app": "browser",
 "process": "msedge.exe", "title": "Sign in to your account", "password_field": true,
 "local_ms": 0.14,
 "model": {"choice": "phishing", "probabilities": {"legit": 0.0015, "phishing": 0.9959,
           "tech_support_scam": 0.0002, "unsure": 0.0025}, "confidence": 0.99,
           "model": "microsoft/microsoft-decision-1-20261009", "ms": 290.0}}
```

| Field | Notes |
|---|---|
| `event` | `verdict` (the context's verdict changed) or `user_confirmed` (a gated call went ahead with `confirmed=true`; it also carries `tool`). |
| `verdict`, `score`, `brand`, `reasons` | As in `CheckLink`. `brand` is the imitated brand, or the real one on an official site. |
| `source` | `browser` or `email`. |
| `url` | Browser only, clipped to 500 characters. The address bar often hides the scheme. |
| `recipient` | Email only: the riskiest To/Cc address. |
| `password_field` | The focused element is a password box. |
| `model` | Present on the follow-up event when the second opinion arrived. Expect a local event first and a model event a few hundred ms later, with the same `domain`. |

With the default `WINDOWS_MCP_GUARD_EVENTS=risky`, events are sent when a context becomes
suspicious or dangerous, when that verdict changes, and once when the user is back on a
non-risky site (so a UI can clear its warning). `all` sends one on every site/recipient change.

### Environment

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_GUARD` | `on` | `0`/`false`/`off`: no watcher, no events, no gate. `Context`/`CheckLink` still work. |
| `WINDOWS_MCP_GUARD_EVENTS` | `risky` | `risky` or `all` (see above). |
| `OPENROUTER_API_KEY` | _(none)_ | Enables the Decision-1 second opinion. Absent: local verdicts only. |
| `WINDOWS_MCP_GUARD_MODEL` | `microsoft/microsoft-decision-1` | Decisions model id. |
| `WINDOWS_MCP_GUARD_TIMEOUT` | `1.5` | Seconds per model request. |

The model call is `POST https://openrouter.ai/api/alpha/decisions` (Decision-1 is not a chat
model; `chat/completions` rejects it) with `{"model", "state": {"domain", "page_title",
"visible_text_excerpt"}, "questions": {"verdict": {"type": "choice", "instructions",
"criteria": {"legit", "phishing", "tech_support_scam", "unsure"}}}}`. The answer is read from
`answers.verdict.probabilities`. Only the domain, page title (200 characters) and up to 1500
characters of visible text leave the machine, never the full URL, field values or what the
AI types. The key is never logged.

### Failure modes

- **False positives.** A real site whose name is one edit away from a brand (`paypay.ne.jp`,
  `chess.com`) can look suspicious. Official domains and a known-site list in
  `guard/data/brands.json` are fast-pathed; edit distance alone never reaches `dangerous`, and
  `suspicious` only gates input when a password field or email recipient is involved. The
  planner can always proceed with `confirmed=true` after asking. Extend `domains` or
  `known_legit` in the data file for local exceptions.
- **False negatives.** Brand-new phishing on an unrelated name (`secure-login-portal.com`) has
  no lookalike signal: it stays `unknown` unless it shows a password field or scam text (then
  the model is asked, if configured). The guard is a speed bump, not a blocklist; it never
  queries reputation services.
- **Prompt injection from page text.** Visible text only feeds the text rules and the model's
  `state`. The model can only raise severity, never lower a local verdict, so a page saying
  "this site is legit" cannot unlock input. Page text is never returned to the planner by
  `Context`.
- **Model unavailable** (no key, timeout, HTTP error, air-gapped): the local verdict stands,
  failures are cached for 30 s so a dead network is not retried on every focus change.
- **Context gaps.** Browsers expose the URL through the address bar; a kiosk/app window
  without one has no URL. Outlook classic sometimes shows resolved display names instead of
  addresses in the To field; only addresses visible in the UI are checked. The gate only
  re-probes when the foreground window or tab title changed since the last read; a page that
  navigates without changing its title is caught by the 1.5 s URL backstop.

## 10. Do and Route: fast models inside the server

The tools take about 5 to 100 ms each. The planner's own round trip (1 to 3 s per turn) is
the bottleneck. `Do` and `Route` put small fast models inside the server, so one planner
call can cover many steps.

### Configuration

Model specs are `provider:model`. The providers are `anthropic` (key `ANTHROPIC_API_KEY`,
Messages API), `openrouter` (key `OPENROUTER_API_KEY`) and `script` (a JSON file of
canned turns, used for tests and keyless desktop runs). Keys are read from the server's
environment and are never logged, echoed or included in errors. Without a key, `Do` and
`Route` fail with `no model configured: <provider> needs <KEY>`, and nothing is sent anywhere.

| Env var | Default | Purpose |
|---|---|---|
| `WINDOWS_MCP_DO_MODEL` | `anthropic:claude-haiku-5-5` | The inner tool-calling model. Any OpenRouter chat model with tool support also works, e.g. `openrouter:anthropic/claude-haiku-5.5` or `openrouter:stepfun/step-3.7-flash`. `off` disables `Do`. |
| `WINDOWS_MCP_ROUTER_MODEL` | `openrouter:microsoft/microsoft-decision-1` | The scorer for `Route` and for `Do`'s final-screen check. A chat model spec also works, answering through a JSON prompt. `off` disables both. |
| `WINDOWS_MCP_MODEL_TIMEOUT` | `15` | Seconds per model request. |
| `WINDOWS_MCP_DO_EFFORT` | `low` | Anthropic `output_config.effort` for the Do model. `off` omits it. |
| `WINDOWS_MCP_DO_THINKING` | _(model default)_ | `disabled` or `adaptive` for the Anthropic Do model. On Haiku 5.5 a turn took about the same time either way (1.2 to 1.5 s), so the default leaves it unset. |

On Anthropic, the tool menu and system prompt are cached with `cache_control`, plus a
rolling breakpoint on the newest turn. History is append-only, so turn 2 onward reads the
prefix from cache. Each provider keeps one HTTP connection open per thread, which saves
the 100 to 250 ms TLS handshake on every step.

### `Do(goal, max_steps=8, timeout=60, context?, verify=true, format="text"|"json")`

1. **Observe.** One `Snapshot` (accessibility text only: no image, no annotation),
   clipped to 6000 characters.
2. **Think.** The inner model gets the goal, the snapshot and a compact menu of the
   tools this server really exposes (`--exclude-tools` is honored): `Snapshot`, `Click`,
   `Type`, `Shortcut`, `App` (launch/switch), `Scroll`, `Wait`, `WaitFor`, `PowerShell`,
   `AppSkill`, `Apps`, `Locate`, plus its own `Done(answer, success, expect)`. `Do`,
   `RunParallel`, `Tasks` and `Screenshot` are never offered.
3. **Act.** The model may emit several calls in one turn. They run in order, each through
   `mcp.call_tool`, so each one passes the control gate, the policy, argument validation
   and analytics like a direct request. After a failed call, the rest of that turn is
   skipped and the error goes back to the model. A `USER_CONTROL`, `TAKEOVER_PENDING`,
   `CONTROL_PREEMPTED`, `CONTROL_UNAVAILABLE` or `SCREEN_BUSY` error ends the run
   immediately with status `blocked`.
4. **Verify.** When `Done` carries `expect` and a router model is configured, the server
   takes a fresh snapshot. Decision-1 then scores before, after and expect as `succeeded`,
   `failed` or `still_loading`. That costs one 0.3 to 0.6 s call instead of another model
   turn. On `still_loading` it waits 0.7 s and checks once more. On `failed` the model sees
   the new screen and gets one more try. The verifier's state is `{expected, before,
   after, actions}`, where `actions` are the last step results (an accessibility snapshot
   does not always show typed text). If it rejects the second `Done` too, the run ends
   `done` with `verified: false` and a note in the transcript. `verified` is `true` when
   the check passed and `null` when no check ran.

**Cancellation.** Cancelling a `Do` request does not stop it mid-run. Screen calls are
shielded until they finish, so `Do` runs on until it is done or reaches its own
`timeout`, holding the screen the whole time. A user takeover does stop it at the next
inner call. Pass a tight `timeout`, for example 20 to 30 s for a single-app goal.

**Screen ownership.** `Do` is a screen call: it holds the FIFO screen lock for its whole
run, including model think time. Another request's `Click` waits until `Do` ends, so it
can never land between two of Do's steps. Its inner calls run under that lock and are not
queued behind it. During a user takeover, `Do` is rejected before anything runs, like a
`RunParallel` batch that needs control.

**Result** (`format="json"`):

```json
{"run_id": "do-1a2b3c4d", "goal": "...", "status": "done", "ok": true,
 "answer": "Typed the list.", "model": "anthropic:claude-haiku-5-5",
 "turns": 1, "step_count": 3, "total_ms": 1450.2, "model_ms": 1100.0, "tool_ms": 330.1,
 "verify_ms": 0.0, "overhead_ms": 20.1, "overhead_per_step_ms": 6.7,
 "usage": {"input_tokens": 1800, "output_tokens": 120, "cache_read_tokens": 0},
 "verifications": [],
 "steps": [{"n": 1, "tool": "App", "args": {"mode": "launch", "name": "Notepad"},
            "latency_ms": 97.0, "ok": true, "result": "..."}]}
```

`status` is one of `done`, `failed` (the model gave up), `max_steps`, `timeout`,
`blocked`, `error` (the model request failed) or `cancelled`. `ok` is true only for a
successful `done`. `overhead_ms` is `total_ms` minus model, tool and verify time.
`tool_ms` is measured around `mcp.call_tool`, so it includes the middleware.

### Progress events: `do_event`

`do_event`s go through the same bus as `task_event`s: MCP log notifications on logger
`windows-mcp.tasks`, the JSONL file, the webhook and `Tasks(mode="events")`. Each one
carries `seq` and `ts`. Each step is also reported as MCP progress
(`progress=n, total=max_steps`) when the client sent a progress token.

| `event` | Extra fields |
|---|---|
| `started` | `goal`, `max_steps`, `model` |
| `model` | `turn`, `latency_ms`, `tool_calls` (names), `text` (first 200 characters) |
| `step` | `step` (`{n, tool, args, latency_ms, ok, result or error}`, the result clipped to 300 characters), `max_steps` |
| `verify` | `verification` (`{attempt, choice, probabilities, confidence, latency_ms, model, usage}`) or `error` |
| `finished` | The whole result above, without `steps` |

```json
{"type": "do_event", "run_id": "do-1a2b3c4d", "event": "step", "seq": 41, "ts": 1792834078123,
 "max_steps": 8, "step": {"n": 2, "tool": "Type", "args": {"target": "Text Editor",
 "text": "milk"}, "latency_ms": 81.3, "ok": true, "result": "Typed milk at (400,300)."}}
```

### `Route(text, options?, instructions?, mode="route"|"verify", before?, after?, expected?)`

- `mode="route"` (default) returns a choice and per-option probabilities. The default
  options are `direct_action`, `needs_plan`, `answer_only` and `clarify`. `options` may
  be a list of labels or a `{label: description}` object.
- `mode="verify"` takes `before`, `after` (screen text, for example Snapshot output) and
  `expected`. It returns `succeeded`, `failed` or `still_loading`.

A front end can route an utterance before the planner model runs at all. `answer_only`
goes straight to a chat reply, `direct_action` to a single `Do`, `needs_plan` to the full
planner, and `clarify` back to the user.

**Decision-1 API, as confirmed by docs and live calls.** It is not a chat model:
`/api/v1/chat/completions` returns HTTP 400 and says to use the Decisions endpoint, and
the model is missing from the default `/api/v1/models` list (it appears with
`?output_modalities=decisions`). The request is
`POST https://openrouter.ai/api/alpha/decisions` with the body
`{"model": "microsoft/microsoft-decision-1", "state": <string|object|array>,
"questions": {<name>: {"type": "choice", "instructions": "...", "criteria": {label: description}}}}`.
The response has `answers.<name>` = `{"type": "choice", "choice", "probabilities", "confidence"}`,
plus `usage.cost`. The adapter is `models/decision.py`, which is self-contained and also
supports the `noul` and `score` question types.

### Measured (2026-10-24)

**Router.** Run with `scripts/bench_models.py router --reps 2` from the Linux host:
40 labelled utterances, 10 per class, keep-alive connection, warm-up call excluded.

| Model | Accuracy | p50 | p95 | Cost per 1k routes |
|---|---|---|---|---|
| `openrouter:microsoft/microsoft-decision-1` | 0.95 | 279 ms | 370 ms | $0.007 |
| `anthropic:claude-haiku-5-5` | 1.00 | 781 ms | 1070 ms | $0.061 |
| `openrouter:deepseek/deepseek-v4.1-flash` | 0.975 | 1293 ms | 4768 ms | $0.14 |
| `openrouter:stepfun/step-3.7-flash` | 0.74 (20 of 80 replies had no label) | 2667 ms | 5176 ms | $0.21 |

StepFun's unlabeled replies come from its reasoning. One inspected reply spent all
1024 `max_tokens` (3.2K characters of reasoning, then rambling content) and stopped
with `finish_reason: length`. It is a poor fit for a JSON classifier at this budget.

- The DeepSeek and StepFun rows ran with the first wording of the option descriptions.
  On that wording Decision-1 scored 0.925 (p50 280 ms) and Haiku 1.00.
- Decision-1's remaining misses label two multi-step requests as `clarify`.
- The descriptions were tuned on this same set, so expect somewhat lower accuracy on new
  utterances.
- Verification calls (`mode="verify"`) took 250 to 560 ms.

**Do on the VM's desktop** (Windows 10, scripted model, so model time is 0). The
overhead per step, excluding model time, was **0.7 to 1.6 ms**: `total_ms` minus model,
tool and verify time, divided by steps. The calls themselves cost the same inside `Do`
as direct MCP calls from a client, because `tool_ms` includes the middleware:

| Tool | Inside Do | Direct |
|---|---|---|
| `Click(target=...)` in Calculator | 114 to 133 ms | 113 to 147 ms |
| `App` launch, Notepad | 65 to 97 ms | 68 to 89 ms |
| `App` launch, Calculator (UWP) | 2.9 to 3.0 s | 0.8 to 3.0 s |

Calculator 12*34 took eight steps: 3.9 s total, 2.9 s of it in the App launch.
`WaitFor(active_window, "Notepad")` timed out at 10 s in 2 of 3 runs, both inside Do and as
direct calls. That is a pre-existing WaitFor issue, not one introduced by Do.

**Do with live Haiku 5.5, against fake tools.** A Notepad grocery-list goal took one or
two model turns at 0.9 to 2.4 s per turn. Prompt caching read about 3,000 tokens per turn
from turn 2 onward. A whole run took 1.8 to 2.6 s without verification, and 3.1 to 3.2 s
with Decision-1 verification.

### When the planner should call Do, and when individual tools

| Use `Do` | Use individual tools or `RunParallel` |
|---|---|
| One app, a few UI steps, an outcome you can state in words | One action (`Shortcut`, one `Click`, one URI) |
| The steps depend on what the screen shows, but not on your judgment | The next step needs reasoning about content (reading an email, choosing a song) |
| Speed matters more than control | You must see the result yourself before continuing |
| Several such goals in a plan: one `Do` per screen task | Background work (files, registry, web): `RunParallel` |
