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
| `Screenshot` | background | Fast capture: windows, cursor, image. The cheapest way to look. |
| `Snapshot` | background | Full UI tree with element ids (labels); `use_dom=True` for browsers. Slower. |
| `Locate` | background | Read-only: find the element a phrase like "Save button" refers to, without clicking. |
| `Scrape` | background | Fetch a web page as markdown (SSRF-protected). |
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
| `Apps` / `AppSkill` | n/a | **In progress on branch `feat/app-skills`.** Per-app skills (URI schemes, CLI/COM recipes, known shortcuts) so the planner can do common app actions without UI exploration. |

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
4. For UI steps, act by name: Click(target="Save button"), Type(target="search box",
   text=...), "File > Save As" paths. Use Locate to check a target without clicking. Use
   WaitFor instead of repeated screenshots. Take a Screenshot only when you are unsure what
   is on screen; use Snapshot only when you need element labels.
5. After each RunParallel, call Tasks(mode="next") only if the next step is not obvious.
   Retry a failed task once with a different route, then mark it failed with a clear error.
6. When the plan is done, reply to the user in one or two sentences.
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
