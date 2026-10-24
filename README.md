<div align="center">
  <h1>🪟 Windows-MCP</h1>

  <a href="https://github.com/CursorTouch/Windows-MCP/blob/main/LICENSE.md">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </a>
  <img src="https://img.shields.io/badge/python-3.14%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/platform-Windows%2010–11-blue" alt="Platform: Windows 10 and 11">
  <img src="https://img.shields.io/github/last-commit/CursorTouch/Windows-MCP" alt="Last Commit">
  <a href="https://pepy.tech/projects/windows-mcp">
    <img src="https://static.pepy.tech/personalized-badge/windows-mcp?period=total&amp;units=INTERNATIONAL_SYSTEM&amp;left_color=BLACK&amp;right_color=GREEN&amp;left_text=downloads" alt="PyPI Downloads">
  </a>
  <br>
  <a href="https://x.com/CursorTouch">
    <img src="https://img.shields.io/badge/follow-%40CursorTouch-1DA1F2?logo=twitter&style=flat" alt="Follow on Twitter">
  </a>
  <a href="https://discord.com/invite/Aue9Yj2VzS">
    <img src="https://img.shields.io/badge/Join%20on-Discord-5865F2?logo=discord&logoColor=white&style=flat" alt="Join us on Discord">
  </a>

  <a href="https://trendshift.io/repositories/20935?utm_source=trendshift-badge&amp;utm_medium=badge&amp;utm_campaign=badge-trendshift-20935" target="_blank" rel="noopener noreferrer"><img src="https://trendshift.io/api/badge/trendshift/repositories/20935/daily?language=Python" alt="CursorTouch%2FWindows-MCP | Trendshift" width="250" height="55"/></a>

</div>

**Windows-MCP** is a lightweight, open-source project that enables seamless integration between AI agents and the Windows operating system. Acting as an MCP server bridges the gap between LLMs and the Windows operating system, allowing agents to perform tasks such as **file navigation, application control, UI interaction, QA testing,** and more.

mcp-name: io.github.CursorTouch/Windows-MCP

## Updates
- Windows-MCP reached `2M+ Users` in [Claude Desktop Extensiosn](https://claude.ai/directory). 
- Try out [🪟Windows-Use](https://pypi.org/project/windows-use/), an agent built using Windows-MCP.
- Windows-MCP is now available on [PyPI](https://pypi.org/project/windows-mcp/) (thus supports `uvx windows-mcp`)
- Windows-MCP is added to [MCP Registry](https://github.com/modelcontextprotocol/registry)

### Supported Operating Systems

- Windows 10
- Windows 11

The current package requires Python 3.14+, which [supports Windows 10 and newer](https://docs.python.org/3.14/using/windows.html#supported-versions). Earlier Windows versions are not supported by the current installation.

## 🎥 Demos

<https://github.com/user-attachments/assets/d0e7ed1d-6189-4de6-838a-5ef8e1cad54e>

<https://github.com/user-attachments/assets/d2b372dc-8d00-4d71-9677-4c64f5987485>

## ✨ Key Features

- **Seamless Windows Integration**  
  Interacts natively with Windows UI elements, opens apps, controls windows, simulates user input, and more.

- **Use Any LLM (Vision Optional)**
   Unlike many automation tools, Windows-MCP doesn't rely on any traditional computer vision techniques or specific fine-tuned models; it works with any LLMs, reducing complexity and setup time.

- **Rich Toolset for UI Automation**  
  Includes tools for basic keyboard, mouse operation and capturing window/UI state.

- **Lightweight & Open-Source**  
  Minimal dependencies and easy setup with full source code available under MIT license.

- **Customizable & Extendable**  
  Easily adapt or extend tools to suit your unique automation or AI integration needs.

- **Real-Time Interaction**  
  Typical latency between actions (e.g., from one mouse click to the next) ranges from **0.2 to 0.5 secs**, and may slightly vary based on the number of active applications and system load, also the inferencing speed of the llm.

- **DOM Mode for Browser Automation**  
  Special `use_dom=True` mode for State-Tool that focuses exclusively on web page content, filtering out browser UI elements for cleaner, more efficient web automation. Supports Chrome, Edge, and Firefox (Firefox uses an IAccessible2 fallback since it doesn't expose `RootWebArea` via UIA).

## 🛠️Installation

**Note:** When you install this MCP server for the first time it may take a minute or two because of installing the dependencies in `pyproject.toml`. In the first run the server may timeout ignore it and restart it.

### Prerequisites

- Python 3.14+
- UV (Package Manager) from Astra, install with `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`
- The `App` tool resolves localized Start Menu display names as well as internal app names; an English Windows display language is not required.

### Run at Login

Run the server directly when needed:

```shell
uvx windows-mcp serve
uvx windows-mcp serve --transport sse --host localhost --port 8000
uvx windows-mcp serve --transport streamable-http --host localhost --port 8000
```

Install it as a background task that starts now and at every login:

```shell
windows-mcp install

# Or choose the HTTP transport and bind address explicitly
windows-mcp install --transport sse --host 127.0.0.1 --port 8000
```

This creates a per-user Scheduled Task named `windows-mcp-server` and a wrapper script at
`~/.windows-mcp/start-server.cmd`. Use `windows-mcp uninstall` to remove it. Logs are written
to `~/.windows-mcp/server.log` and `~/.windows-mcp/server.error.log`.

<details>
  <summary>Install in Claude Desktop</summary>

  1. Install [Claude Desktop](https://claude.ai/download).

```shell
npm install -g @anthropic-ai/mcpb
```

  2. Configure the MCP server.

  **Option A: Install from PyPI (Recommended)**
  
  Use `uvx` to run the latest version directly from PyPI.

  Add this to your `claude_desktop_config.json`:
  ```json
  {
    "mcpServers": {
      "windows-mcp": {
        "command": "uvx",
        "args": [
          "windows-mcp",
          "serve"
        ]
      }
    }
  }
  ```

  **Option B: Install from Source**

  1. Clone the repository:
  ```shell
  git clone https://github.com/CursorTouch/Windows-MCP.git
  cd Windows-MCP
  ```

  2. Add this to your `claude_desktop_config.json`:
  ```json
  {
    "mcpServers": {
      "windows-mcp": {
        "command": "uv",
        "args": [
          "--directory",
          "<path to the windows-mcp directory>",
          "run",
          "windows-mcp",
          "serve"
        ]
      }
    }
  }
  ```
  3. Fully restart Claude Desktop and verify the server appears in the MCP tools list.

  **Claude Desktop MSIX (Windows Store)**

  The MSIX-packaged Claude Desktop (Microsoft Store version) virtualizes `%APPDATA%`. This causes two main issues:
  1. The config file is located at: `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` (not `%APPDATA%\Claude\`).
  2. Automatic installation from the "Claude Directory" will fail because the `${__dirname}` variable resolves to the incorrect (non-virtualized) path.

  **To configure Windows-MCP on the Windows Store version of Claude:**
  
  You must manually edit the configuration file. Note that Electron apps in the MSIX sandbox do not inherit the system `PATH`, so you must use the **full absolute path** to `uvx.exe` (or `uv.exe`).

  **Option A: Using pre-installed executable**

  1. In a terminal, run `uv tool install windows-mcp`.
  2. Use the generated executable in your config:
  ```json
  {
    "mcpServers": {
      "windows-mcp": {
        "command": "C:\\Users\\<user>\\.local\\bin\\windows-mcp.exe",
        "args": ["serve"]
      }
    }
  }
  ```

  **Option B: Using uvx**
  ```json
  {
    "mcpServers": {
      "windows-mcp": {
        "command": "C:\\Users\\<user>\\.local\\bin\\uvx.exe",
        "args": ["windows-mcp", "serve"]
      }
    }
  }
  ```

  **Option C: Install from Source**
  ```json
  {
    "mcpServers": {
      "windows-mcp": {
        "command": "C:\\Users\\<user>\\.local\\bin\\uv.exe",
        "args": [
          "--directory",
          "C:\\path\\to\\Windows-MCP",
          "run",
          "windows-mcp",
          "serve"
        ]
      }
    }
  }
  ```

  Replace `<user>` with your Windows username. To find the correct paths, run `where uvx`, `where windows-mcp`, or `where uv`. Fully quit Claude Desktop (Tray → Quit) and reopen after saving the config.

  For additional Claude Desktop integration troubleshooting, see the [MCP documentation](https://modelcontextprotocol.io/quickstart/server#claude-for-desktop-integration-issues).
</details>

<details>
  <summary>Install in Perplexity Desktop</summary>

  1. Install [Perplexity Desktop](https://apps.microsoft.com/detail/xp8jnqfbqh6pvf).
  2. Open Perplexity Desktop and go to `Settings -> Connectors -> Add Connector -> Advanced`.
  3. Enter the name as `Windows-MCP`, then paste one of the following configs.


  **Option A: Install from PyPI (Recommended)**

  ```json
  {
    "command": "uvx",
    "args": [
      "windows-mcp",
      "serve"
    ]
  }
  ```

  **Option B: Install from Source**

  ```json
  {
    "command": "uv",
    "args": [
      "--directory",
      "<path to the windows-mcp directory>",
      "run",
      "windows-mcp",
      "serve"
    ]
  }
  ```

  4. Click `Save`, then restart Perplexity Desktop if needed.

For additional Claude Desktop integration troubleshooting, see the [Perplexity MCP Support](https://www.perplexity.ai/help-center/en/articles/11502712-local-and-remote-mcps-for-perplexity). The documentation includes helpful tips for checking logs and resolving common issues.
</details>

<details>
  <summary> Install in Gemini CLI</summary>

  1. Install Gemini CLI.

```shell
npm install -g @google/gemini-cli
```

  2. Open `%USERPROFILE%/.gemini/settings.json`.
  3. Add the `windows-mcp` config and save it.

```json
{
  "theme": "Default",
  ...
  "mcpServers": {
    "windows-mcp": {
      "command": "uvx",
      "args": [
        "windows-mcp",
        "serve"
      ]
    }
  }
}
```
*Note: To run from source, replace the command with `uv` and args with `["--directory", "<path>", "run", "windows-mcp", "serve"]`.*

  4. Restart Gemini CLI.
</details>

<details>
  <summary>Install in Qwen Code</summary>
  1. Install Qwen Code.

```shell
npm install -g @qwen-code/qwen-code@latest
```
  2. Open `%USERPROFILE%/.qwen/settings.json`.
  3. Add the `windows-mcp` config and save it.

```json
{
  "mcpServers": {
    "windows-mcp": {
      "command": "uvx",
      "args": [
        "windows-mcp",
        "serve"
      ]
    }
  }
}
```
*Note: To run from source, replace the command with `uv` and args with `["--directory", "<path>", "run", "windows-mcp", "serve"]`.*

  4. Restart Qwen Code.
</details>

<details>
  <summary>Install in Codex CLI</summary>
  1. Install Codex CLI.

```shell
npm install -g @openai/codex
```
  2. Open `%USERPROFILE%/.codex/config.toml`.
  3. Add the `windows-mcp` config and save it.

```toml
[mcp_servers.windows-mcp]
command="uvx"
args=[
  "windows-mcp",
  "serve"
]
```
*Note: To run from source, replace the command with `uv` and args with `["--directory", "<path>", "run", "windows-mcp", "serve"]`.*

  4. Restart Codex CLI.
</details>

<details>
  <summary>Install in Autohand Code</summary>

  Add the published stdio server from a Windows terminal:

  ```shell
  autohand mcp add windows-mcp uvx windows-mcp serve
  ```

  Add `--scope project` after `add` to keep the server configuration in the current project. See [Autohand Code](https://github.com/autohandai/code-cli/) for current installation and CLI details.
</details>

<details>
  <summary>Install in Claude Code</summary>

  1. Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview):

```shell
npm install -g @anthropic-ai/claude-code
```

  2. Configure the server:

  **Option A: Install from PyPI (Recommended)**

  Use `uvx` to run the latest version directly from PyPI.

  ```shell
  claude mcp add --transport stdio windows-mcp -- uvx windows-mcp serve
  ```

  **Option B: Install from Source**

  1. Clone the repository:
  ```shell
  git clone https://github.com/CursorTouch/Windows-MCP.git
  cd Windows-MCP
  ```

  2. Run the following command in your terminal:
  ```shell
  claude mcp add --transport stdio windows-mcp -- uv --directory "<path>" run windows-mcp serve
  ```

  *Note: To make the server available across all projects, add `--scope user` to the command.*

  3. Rerun Claude Code in terminal. Enjoy 🥳

  **Note:** On Windows, if you encounter "Connection closed" errors, use the full path to `uvx.exe`:

  ```shell
  claude mcp add --transport stdio windows-mcp -- C:\Users\<user>\.local\bin\uvx.exe windows-mcp serve
  ```

  To verify the server is registered, run `claude mcp list`. Inside Claude Code, use `/mcp` to check server status.

  **WSL (Windows Subsystem for Linux)**

  If you run Claude Code from WSL, the MCP server must still execute on the Windows side (it needs Windows APIs for UI automation). Use `powershell.exe` as the command to bridge WSL and Windows:

  1. Install `uv` on **Windows** (from a PowerShell terminal):
  ```powershell
  irm https://astral.sh/uv/install.ps1 | iex
  ```

  2. From your **WSL terminal**, register the server:
  ```shell
  claude mcp add windows-mcp --transport stdio -s user -- powershell.exe -Command "C:\Users\<user>\.local\bin\uvx.exe windows-mcp serve"
  ```

  Replace `<user>` with your Windows username. The `-s user` flag makes the server available across all projects.

  3. Restart Claude Code and verify with `/mcp`.
</details>

---

## 🖥️ Running Windows-MCP

Windows-MCP runs directly on your Windows machine and exposes its tools to the connected MCP client.

```shell
# Runs with stdio transport (default)
uvx windows-mcp serve

# Or with SSE/Streamable HTTP for network access
uvx windows-mcp serve --transport sse --host localhost --port 8000
uvx windows-mcp serve --transport streamable-http --host localhost --port 8000
```

Optional environment variables can be set to customize behavior — see [Environment Variables](#-environment-variables) below.

### Security for Remote Access

For network access, enable authentication and TLS:

```shell
windows-mcp serve --transport sse --host 0.0.0.0 \
  --auth-key "your_secret_token" \
  --ip-allowlist "203.0.113.0/24" \
  --ssl-certfile cert.pem --ssl-keyfile key.pem
```

See [🔐 Security & Access Control](#-security--access-control) for all options.

### Transport Options

| Transport | Command | Use Case |
|---|---|---|
| `stdio` (default) | `serve --transport stdio` | Direct connection from MCP clients like Claude Desktop, Cursor, etc. |
| `sse` | `serve --transport sse --host HOST --port PORT` | Network-accessible via Server-Sent Events |
| `streamable-http` | `serve --transport streamable-http --host HOST --port PORT` | Network-accessible via HTTP streaming (recommended for production) |

---

## 🔐 Security & Access Control

### Authentication
```shell
windows-mcp serve --transport sse --host 0.0.0.0 --auth-key "your_token"
```
Requires `Authorization: Bearer your_token` header on all requests.

### IP Allowlist
```shell
windows-mcp serve --auth-key "token" --ip-allowlist "203.0.113.0/24,198.51.100.5"
```
Restricts connections to specified CIDR ranges. Blocks private/loopback IPs by default.

### CORS Origins

By default, **no CORS headers are emitted**. Browsers block cross-origin requests via their own Same-Origin Policy, which means arbitrary websites cannot reach the MCP control plane even if the server is on `localhost`. Host-header validation (DNS rebinding protection) is also applied automatically based on the bind address.

If you need a browser-based MCP client to reach the server, opt in with an explicit origin allowlist:

```shell
windows-mcp serve --cors-origins "https://my-client.example.com,https://other.example.com"
```

Only the listed origins receive `Access-Control-Allow-Origin` headers; all other cross-origin requests are rejected by the browser. The equivalent environment variable is `WINDOWS_MCP_CORS_ORIGINS`.

### Tool Selection
All tools are enabled by default. Use `--tools` to whitelist specific tools, or `--exclude-tools` to block specific ones.

```shell
windows-mcp serve --tools "Screenshot,Click,Snapshot"   # Enable only these tools
windows-mcp serve --exclude-tools "PowerShell,Registry" # Disable specific tools
```

### TLS/HTTPS
```shell
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes

windows-mcp serve --ssl-certfile cert.pem --ssl-keyfile key.pem
```

### OAuth 2.0 + PKCE

For MCP clients that use OAuth (e.g. Claude Desktop) instead of a static API key:

```shell
windows-mcp serve --transport streamable-http --host 0.0.0.0 \
  --ssl-certfile ~/.windows-mcp/cert.pem \
  --ssl-keyfile  ~/.windows-mcp/key.pem \
  --oauth-client-id my-client \
  --oauth-client-secret my-secret
```

**Claude Desktop config:**
```json
{
  "mcpServers": {
    "windows-mcp": {
      "type": "http",
      "url": "https://<host>:8000/mcp/",
      "oauth": {
        "clientId": "my-client",
        "clientSecret": "my-secret"
      }
    }
  }
}
```

The OAuth server exposes:
- `GET /.well-known/oauth-authorization-server` — server metadata (RFC 8414)
- `GET /oauth/authorize` — Authorization Code + PKCE (`S256` required)
- `POST /oauth/token` — token exchange (client secret required)
- `POST /oauth/register` — disabled; clients must be pre-provisioned

Dynamic client registration is disabled. Redirect URIs must be loopback `http(s)` only.
Auth key and OAuth can coexist — both are accepted as valid Bearer tokens.

### Config File (`~/.windows-mcp/config.toml`)

Instead of passing flags every time, store your configuration in `~/.windows-mcp/config.toml`. CLI flags always override config file values.

**Search order:**
1. `--config /path/to/config.toml`
2. `~/.windows-mcp/config.toml`

**stdio** — local only, no security needed:
```toml
[server]
transport = "stdio"
```

**SSE** — network access with auth and IP restriction:
```toml
[server]
transport = "sse"
host      = "0.0.0.0"
port      = 8000
auth_key  = "your-secret-key"

[security]
ip_allowlist = ["192.168.1.0/24"]
```

**Streamable HTTP** — with auth, TLS, and tool exclusions:
```toml
[server]
transport    = "streamable-http"
host         = "0.0.0.0"
port         = 8000
auth_key     = "your-secret-key"
ssl_certfile = "cert.pem"   # resolved relative to ~/.windows-mcp/
ssl_keyfile  = "key.pem"

[security]
ip_allowlist        = ["192.168.1.0/24"]
cors_origins        = ["https://my-client.example.com"]   # optional — browser CORS opt-in
oauth_client_id     = "my-client"      # optional — enables OAuth 2.0 + PKCE
oauth_client_secret = "my-secret"

[tools]
exclude = ["PowerShell", "Registry"]   # disable specific tools
```

Place cert and key files in the same directory:

```
~/.windows-mcp/
├── config.toml
├── cert.pem
└── key.pem
```

Generate a self-signed cert directly into that directory:

```shell
mkdir -p ~/.windows-mcp
openssl req -x509 -newkey rsa:4096 \
  -keyout ~/.windows-mcp/key.pem \
  -out ~/.windows-mcp/cert.pem \
  -days 365 -nodes
```

### Desktop control

Desktop control is enabled by default with the **relaxed** policy: direct desktop
actions are gated, while background tools can run as you use the computer.
Configure it in `~/.windows-mcp/config.toml` (or disable it entirely with `enabled = false`):

```toml
[control]
enabled = true
policy = "relaxed"
idle_timeout_seconds = 10.0
```

Or override it for one MCP server launch:

```shell
windows-mcp serve --no-control
windows-mcp serve --control --control-idle-timeout 3
windows-mcp serve --control-policy strict
```

In your MCP client's launch arguments, put these options after `serve`.
The priority is CLI > environment variable > TOML > default. `--control` can
explicitly override `enabled = false`. The timeout accepts finite positive seconds
(including fractions); zero does not disable control. The default remains 10 seconds.

Restart the Windows-MCP server process after changing these settings. There is no
hot reload or AI configuration tool. `ControlStatus` reports the effective `enabled`
`policy`, and `idle_timeout_seconds` values, with state `disabled` when control is off.

Disabling control turns off ownership gating, physical input protection, takeover
monitoring, and the control indicator. Tools remain available and serialized, with
injected key/button cleanup retained. AI desktop actions can then interfere with your
own input; PowerShell scripts can also operate the UI. The separate screenshot flash
setting is unchanged. When control is enabled, the idle timer restarts with physical
input, so shortening it does not allow gated calls while you keep using the computer.

During AI control, small mouse movements remain blocked without pausing the current
tool. Mouse takeover adds up the length of each relative raw-input movement,
including back-and-forth movement, until it reaches 500 device units by default.
A gap of 0.5 seconds without movement starts a new segment; switching devices or
starting a new control period also clears the total. Set `control.mouse_takeover_units`
in TOML to change the threshold (40–2000), then restart the server. These are device
units, not screen pixels. Devices that do not provide identifiable relative raw
input, including some touchpads and remote-input devices, must use
`Ctrl+Alt+Shift+Backspace` to take over. The legacy `mouse_takeover_pixels` setting
is still accepted for compatibility but no longer affects takeover.

### Desktop gate policies

| Policy | Operations that require desktop ownership |
|---|---|
| `relaxed` (A, default) | Direct input: Click, Type, Scroll, Move, Shortcut, MultiSelect, MultiEdit; App `switch` and `resize`. |
| `balanced` (B) | Everything in A, plus PowerShell, Notification, AppSkill, App `launch`/`launch_executable`, Process `kill`, and Clipboard `set`. |
| `strict` (C) | All tools except ControlStatus, matching the previous gate behavior. |

FileSystem, Registry, Snapshot, Screenshot, Scrape, Wait, WaitFor, DisplayInventory, Apps,
Process `list`, and Clipboard `get` bypass ownership in A and B. This classification
concerns desktop interference, not permissions or data safety. PowerShell can still
operate the UI, and file/registry operations can have other side effects.

`Tasks` (local task state) is never gated, like `ControlStatus`. `RunParallel` is not gated
itself; each of its inner calls passes the gate individually, and a whole batch is
rejected up front during a user takeover when its strictest inner call needs ownership.

Ungated calls do not acquire or extend the desktop lease, activate its indicator,
or wait for its tool lock. Existing resource constraints still apply: UI Automation
operations share a dedicated worker, so a long WaitFor can delay other UIA work.
The separate screenshot flash is unchanged. `--no-control` retains its existing
all-tools serialization and overrides the policy's ownership checks.

In relaxed mode, agents should prefer background work and coordinate known desktop
interference with the user. Obtain permission before knowingly disrupting a user's
process or taking focus, reusing existing authorization. These are tool-use guidelines,
not server-enforced approval or process ownership tracking. Clipboard continues to
use the real Windows clipboard; no virtual clipboard is introduced.

For contributors: tool categories and the few mode-specific exceptions live in
`src/windows_mcp/tools/control_policy.py`. Update that table when adding tools or modes;
registration coverage tests detect omissions. Unknown extensions conservatively
use the gate. Keep policy decisions out of individual tool implementations.

### `auth` Helper

Generate an auth key and save a working config to `~/.windows-mcp/config.toml`:

```shell
windows-mcp auth
```

Generate auth plus a self-signed TLS certificate:

```shell
windows-mcp auth --transport streamable-http --host 0.0.0.0 --port 8000 --with-tls
```

This command writes the auth key into the config file, can generate `cert.pem` and `key.pem`, and prints an example MCP client configuration for the selected transport.

### SSRF Protection
`Scrape` tool blocks: private IPs, loopback, link-local, credentials-in-URLs, non-HTTP schemes.

---

## ⚙️ Environment Variables

All variables are optional unless noted. Set them via the `env` key in `claude_desktop_config.json` (or your MCP client's equivalent config).

### Screenshot & Snapshot

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_SCREENSHOT_SCALE` | `1.0` | Scale factor applied to screenshots before encoding. Accepts a float in the range `0.1`–`1.0`. Useful on high-resolution displays (1440p, 4K) where the default produces images that exceed Claude Desktop's 1 MB tool-result limit. Set to `0.5` to halve both dimensions (quarter the file size). |
| `WINDOWS_MCP_SCREENSHOT_BACKEND` | `auto` | Screenshot capture backend. Accepted values: `auto` (tries dxcam → mss → pillow in order), `dxcam`, `mss`, `pillow`. Use `mss` or `pillow` if `dxcam` is unavailable or causes issues on your GPU. |
| `WINDOWS_MCP_PROFILE_SNAPSHOT` | _(disabled)_ | Set to `1`, `true`, `yes`, or `on` to emit per-stage timing logs for Screenshot/Snapshot calls. Useful for diagnosing slow captures. |
| `WINDOWS_MCP_DISABLE_FLASH` | _(disabled)_ | Set to `1`, `true`, `yes`, or `on` to suppress the orange-red glowing border that briefly highlights the captured area after every screenshot. The flash is rendered on a transparent always-on-top window *after* capture so it never appears in the captured image. |

### Excluding processes from UI Automation traversal

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_EXCLUDE_PROCESSES` | _(none)_ | Comma-separated process basenames whose windows are excluded from UI Automation tree traversal, e.g. `Code.exe,Cursor.exe`. Matching is case-insensitive and exact — no wildcards or regular expressions. Unset or empty preserves current behaviour. |

Some applications expose accessibility trees that can become slow or unresponsive during UI Automation traversal. `Snapshot` and `WaitFor` may traverse the active window and other top-level handles selected for tree capture, so one pathological accessibility provider can stall the capture.

Set:

```text
WINDOWS_MCP_EXCLUDE_PROCESSES=Code.exe,Cursor.exe
```

Process names are comma-separated executable basenames. They are matched case-insensitively after surrounding whitespace is stripped; empty entries are ignored and duplicates are harmless.

A window is excluded *before* any UI Automation call is made against it, so its accessibility provider is never entered. Excluded applications may still appear in the window list, but Windows-MCP will not traverse their accessibility trees, and they are not reported as failed captures. If the owning process of a window cannot be resolved, the window is traversed as usual.

This is an exclusion mechanism — a mitigation, not a complete fix for every form of [#383](https://github.com/CursorTouch/Windows-MCP/issues/383). It only skips the processes you name; a pathological window you have not listed can still stall a capture.

### MCP host windows and per-window budgets

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_SKIP_HOST_PROCESSES` | `on` | Skip windows owned by the processes that launched Windows-MCP (the MCP host, such as VS Code, Cursor or Claude Desktop). Set to `0`, `false`, `no` or `off` to traverse them. |
| `WINDOWS_MCP_WINDOW_TIME_BUDGET_SEC` | `10` | Seconds a single window's tree walk may take. When it runs out, Windows-MCP keeps the elements found so far in that window and moves on to the next one. `0` disables the budget. Invalid or negative values use the default. |
| `WINDOWS_MCP_MAX_WINDOW_ELEMENTS` | _(no cap)_ | Most elements a single window may contribute, so one huge window cannot use the whole `WINDOWS_MCP_MAX_TREE_ELEMENTS` budget. Unset, `0` or invalid means no per-window cap. |

By default, Windows-MCP does not walk the accessibility tree of the application that launched it. Walking an Electron host's own renderer from inside it can hang the host (seen with VS Code, #383), and Windows then closes it along with every session inside. To find the host, Windows-MCP follows its parent processes up to the first one that owns a visible window, which is the host. Whatever launched the host, such as a browser that opened a `vscode://` link, is still captured. The walk never goes past `explorer.exe` or a system process, so the taskbar, the desktop and File Explorer are always captured. The skipped process names are logged once at startup. Host windows are left out of the tree, but they still appear in the window list. They are not reported as failed captures.

The per-window budgets limit how much time and how many elements one slow or very large window can use. A window that hits its budget returns a partial tree, and every other window is still captured. A window cut short this way is logged as a warning. It does not add the truncation note that `WINDOWS_MCP_MAX_TREE_ELEMENTS` adds.

If your MCP client runs inside Electron and you want to automate its windows too, launch it with Chromium's `--disable-renderer-accessibility` flag (e.g. `code --disable-renderer-accessibility`). The client then stops answering deep UI Automation queries into its web content, which avoids the hang, at the cost of screen-reader support in that app.

### UI Automation timeouts

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_UIA_CONNECTION_TIMEOUT_MS` | `2000` | Milliseconds UI Automation waits to connect to a window's accessibility provider before failing that window. Positive integer; anything else uses the default. |
| `WINDOWS_MCP_UIA_TRANSACTION_TIMEOUT_MS` | `5000` | Milliseconds a single UI Automation call into a provider may take before it fails. Positive integer; anything else uses the default. |

When a window stops answering UI Automation (its UI thread is busy, deadlocked, or still starting up), `Snapshot`, `WaitFor` and other UI-tree tools now skip that window after one timeout. The window is reported as a failed capture, and the rest of the desktop is still returned. Previously the call waited until the window recovered, and every later tool call queued behind it. Raise these values only if an application legitimately needs longer for a single accessibility call. To stop traversing an application at all, use `WINDOWS_MCP_EXCLUDE_PROCESSES`.

### Input and shell latency

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_INPUT_SETTLE_MS` | `30` | Milliseconds to wait after each injected click, key chord or wheel tick before the tool returns. Replaces the fixed 500 ms wait inherited from python-uiautomation, which made every `Click`/`Shortcut` cost over half a second. Accepts `0`–`5000`; invalid values use the default. Set `500` to restore the old timing. Consecutive clicks at the same spot still wait out the system double-click time so they never merge into a double-click, and `Type` polls (up to 200 ms) for the clicked window to take the foreground before sending keys. |
| `WINDOWS_MCP_POWERSHELL_SESSION` | `on` | Run `PowerShell`, `Registry`, `App` and `Notification` commands in one long-lived PowerShell process instead of starting `powershell.exe` (about 1 s) per call. Set to `off`/`0`/`false`/`no` to always start a fresh process. Each command still starts in the home directory with the session's start-up environment (location, `$env:` changes, `$LASTEXITCODE` and `$Error` are reset; plain variables and functions are scoped to the command), but `$global:` variables, imported modules and `Add-Type` types persist. `exit N` returns `N` and restarts the session. A call that times out kills the session and only the processes that call started (apps launched by earlier calls survive); the next call starts a new session. A call that arrives while the session is busy, or when it cannot start, runs in a fresh process as before. Errors come back as plain text instead of Windows PowerShell's CLIXML. |

### Desktop control settings

If a physical key/button release event is missed, the input monitor checks residual
holds after the configured idle wait. Five consecutive all-released samples, at
least 100 ms apart, clear the stale records. New physical input or a reported held
key/button resets confirmation. This is best-effort: a zero `GetAsyncKeyState`
result counts as released even when Windows cannot provide the state; repeated
sampling filters transient readings but cannot distinguish persistent query failure.

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_CONTROL` | `true` | Enable desktop control coordination. Set to `false` to disable. Equivalent to `--control/--no-control`; restart required. |
| `WINDOWS_MCP_CONTROL_POLICY` | `relaxed` | Gate policy: `relaxed`, `balanced`, or `strict`. Equivalent to `--control-policy`; restart required. |
| `WINDOWS_MCP_CONTROL_IDLE_TIMEOUT` | `10.0` | Finite positive idle wait in seconds. Equivalent to `--control-idle-timeout`; restart required. |

### Security

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_AUTH_KEY` | _(none)_ | Bearer token required on all HTTP requests. Alternative to `--auth-key` CLI flag. |
| `WINDOWS_MCP_IP_ALLOWLIST` | _(none)_ | Comma-separated list of allowed client IPs or CIDR ranges (e.g., `203.0.113.0/24,198.51.100.5`). Alternative to `--ip-allowlist` CLI flag. |
| `WINDOWS_MCP_CORS_ORIGINS` | _(none)_ | Comma-separated list of origins permitted to make cross-origin browser requests (e.g., `https://my-client.example.com`). No CORS headers are emitted when unset. Alternative to `--cors-origins` CLI flag. |
| `WINDOWS_MCP_TOOLS` | _(all enabled)_ | Comma-separated explicit list of tools to enable (e.g., `Screenshot,Click,Snapshot`). Alternative to `--tools` CLI flag. |
| `WINDOWS_MCP_EXCLUDE_TOOLS` | _(none)_ | Comma-separated list of tools to disable (e.g., `PowerShell,Registry`). Alternative to `--exclude-tools` CLI flag. |
| `WINDOWS_MCP_SSL_CERTFILE` | _(none)_ | Path to TLS certificate file (.pem) for HTTPS. Must be provided with `WINDOWS_MCP_SSL_KEYFILE`. |
| `WINDOWS_MCP_SSL_KEYFILE` | _(none)_ | Path to TLS private key file (.pem) for HTTPS. Must be provided with `WINDOWS_MCP_SSL_CERTFILE`. |
| `WINDOWS_MCP_OAUTH_CLIENT_ID` | _(none)_ | OAuth client ID for HTTP transports. Must be provided with `WINDOWS_MCP_OAUTH_CLIENT_SECRET`. |
| `WINDOWS_MCP_OAUTH_CLIENT_SECRET` | _(none)_ | OAuth client secret for HTTP transports. Must be provided with `WINDOWS_MCP_OAUTH_CLIENT_ID`. |
| `WINDOWS_MCP_STATELESS_HTTP` | `false` | Set to `1`, `true`, `yes`, or `on` to run `streamable-http` without `Mcp-Session-Id` connection state. Useful for reconnects after restarts and for horizontally scaled deployments. |

[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/cursortouch-windows-mcp-badge.png)](https://mseep.ai/app/cursortouch-windows-mcp)

### Click target resolver

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_RESOLVER_MIN_CONFIDENCE` | `0.7` | `Click`/`Type` with `target=` act only at or above this confidence (`0`–`1`). Below it they click nothing and return the top candidates instead. |
| `WINDOWS_MCP_RESOLVER_VISION` | _(off)_ | Set to `anthropic` (or `on`) to enable the tier-2 vision fallback. Off by default. |
| `WINDOWS_MCP_RESOLVER_MODEL` | `claude-haiku-5-5` | Vision model used by tier 2. The key comes from `ANTHROPIC_API_KEY`. |
| `WINDOWS_MCP_RESOLVER_VISION_TIMEOUT` | `3.0` | Seconds allowed for one tier-2 request. On timeout, HTTP error or a "none" answer the resolver falls back to the tier-1 result and does not click. |

**Privacy note:** tier 1 (accessibility) never leaves the machine. Tier 2 is the only part of the resolver that sends data off the machine: when `WINDOWS_MCP_RESOLVER_VISION` is set *and* accessibility matching is weak (or the window exposes almost no elements), it sends a downscaled JPEG of **the target window only**, with numbered boxes drawn on it, plus the target text, to the configured provider (`api.anthropic.com`). Having `ANTHROPIC_API_KEY` in the environment does not enable it. Leave the variable unset if window contents must not leave the machine.
### Apps & skills

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_APPS_TTL` | `21600` (6 h) | Seconds the installed-app inventory (memory and `%LOCALAPPDATA%\windows-mcp\Cache\apps-inventory.json`) stays fresh before `Apps`/`AppSkill` rebuild it. `Apps(mode="refresh")` rebuilds it immediately. |
| `WINDOWS_MCP_SKILLS_DIR` | `%LOCALAPPDATA%\windows-mcp\skills` | Folder for user and generated skills. Bundled skills always load from the package. |

### Tasks and RunParallel

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_TASKS_FILE` | _(off)_ | Persist task plans as JSON so a restart keeps them. `1`/`true`/`default` uses `tasks.json` in the platformdirs user data dir (`%LOCALAPPDATA%\windows-mcp`); any other value is a file path. Tasks that were `running` at a crash reload as `failed`. |
| `WINDOWS_MCP_TASK_EVENTS_FILE` | _(off)_ | Append every task/plan event as one JSON line to this file. |
| `WINDOWS_MCP_TASK_WEBHOOK` | _(off)_ | POST every event as JSON to this URL from one background thread (fire-and-forget, ordered, bounded queue). |
| `WINDOWS_MCP_TASK_WEBHOOK_TOKEN` | _(none)_ | Sent as `Authorization: Bearer <token>` on webhook posts. |
| `WINDOWS_MCP_TASK_WEBHOOK_TIMEOUT` | `2.0` | Seconds per webhook POST. |
| `WINDOWS_MCP_PARALLEL_MAX` | `4` | Default `RunParallel` background concurrency (max 16). |
| `WINDOWS_MCP_PARALLEL_TIMEOUT` | `60` | Default per-call timeout (seconds) for `RunParallel` background calls. |
| `WINDOWS_MCP_SCREEN_LOCK_TIMEOUT` | `60` | Seconds a screen-touching call waits for the screen lock before failing with `SCREEN_BUSY`. |

### Do and Route (in-server models)

Model specs are `provider:model`, with provider `anthropic` (key `ANTHROPIC_API_KEY`), `openrouter` (key `OPENROUTER_API_KEY`) or `script` (a JSON file of canned turns, for keyless tests). Nothing is sent to a provider until `Do` or `Route` is called.

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_DO_MODEL` | `anthropic:claude-haiku-5-5` | Inner model for `Do` (tool calling). `off` disables `Do`. E.g. `openrouter:anthropic/claude-haiku-5.5`, `openrouter:stepfun/step-3.7-flash`. |
| `WINDOWS_MCP_ROUTER_MODEL` | `openrouter:microsoft/microsoft-decision-1` | Scorer for `Route` and `Do`'s final-screen check. Any chat model spec also works. `off` disables both. |
| `WINDOWS_MCP_MODEL_TIMEOUT` | `15` | Seconds per model request (0.5 to 120). |
| `WINDOWS_MCP_DO_EFFORT` | `low` | Anthropic `output_config.effort` for the `Do` model (`low` to `max`, `off` omits it). |
| `WINDOWS_MCP_DO_THINKING` | _(unset)_ | `disabled` or `adaptive` thinking for the Anthropic `Do` model; unset leaves the model default. |

### Telemetry

| Variable | Default | Description |
|---|---|---|
| `ANONYMIZED_TELEMETRY` | `true` | Set to `false` to disable anonymous usage telemetry. No personal data, tool arguments, or outputs are ever collected regardless of this setting. |
| `POSTHOG_API_KEY` | Project default | Override the PostHog project write key used for anonymous telemetry. Set to an empty string to skip PostHog client initialization. |
| `POSTHOG_HOST` | `https://us.i.posthog.com` | Override the PostHog host for anonymous telemetry, such as for a self-hosted PostHog deployment. |

### Debug

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_DEBUG` | `false` | Set to `1`, `true`, `yes`, or `on` to enable debug mode, which sets the log level to DEBUG for verbose output. Also available as the `--debug` CLI flag. |

### WatchDog

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_WATCHDOG` | `false` | Set to `on`, `1`, `true`, `yes`, or `enabled` (case-insensitive) to run the UIA focus watchdog. It is off by default because its only remaining effect is debug logging of focus changes, while it costs a background thread and a long-lived UIA event subscription that can crash the server on unstable UIA environments after long uptime (e.g. across a sleep/resume or session change). The accessibility tree is built on demand for every tool call either way, so tool behaviour is unaffected. |

**Example `claude_desktop_config.json`:**

Local (no security):
```json
{
  "mcpServers": {
    "windows-mcp": {
      "command": "uvx",
      "args": ["windows-mcp", "serve"],
      "env": { "WINDOWS_MCP_SCREENSHOT_SCALE": "0.5" }
    }
  }
}
```

Remote (with auth + IP allowlist + TLS):
```json
{
  "mcpServers": {
    "windows-mcp": {
      "command": "uvx",
      "args": ["windows-mcp", "serve", "--transport", "sse", "--host", "0.0.0.0"],
      "env": {
        "WINDOWS_MCP_AUTH_KEY": "your_token",
        "WINDOWS_MCP_IP_ALLOWLIST": "203.0.113.0/24",
        "WINDOWS_MCP_SSL_CERTFILE": "/path/to/cert.pem",
        "WINDOWS_MCP_SSL_KEYFILE": "/path/to/key.pem"
      }
    }
  }
}
```

---

## 🔨MCP Tools

MCP Client can access the following tools to interact with Windows:

- `Click`: Click on the screen at the given coordinates, on a Snapshot label, or on a `target` described in words (`Click(target="Save button")`, `Click(target="Seven")`, `Click(target="File > Save As")`). A target is resolved inside the server from the accessibility tree of the foreground window (or `window="Spotify"`), in ~10–40 ms on simple windows and up to ~200 ms on large dialogs (measured on a Win10 VM), so no Snapshot round trip is needed. Optional `control_type` (`button`, `edit`, `menu`, `tab`, `link`, `checkbox`, ...) and `nth` (1-based, reading order) disambiguate. Words like "button", "box"/"field", "tab", "link", "checkbox" and "menu" in the target act as control-type hints. If the best match is below `WINDOWS_MCP_RESOLVER_MIN_CONFIDENCE`, nothing is clicked and the tool returns an error listing the top 3 candidates (name, type, location, score). The result names what was resolved, e.g. `resolved: "Seven" (button) via uia in 23 ms, confidence 1.00`.
- `Locate`: Read-only counterpart of `Click(target=...)`: returns the best match and top candidates without clicking. For a path only the first step can be located.
- `Type`: Type text on an element (optionally clears existing text). Accepts `target` (e.g. `Type(target="search box", text="cats")`) to find the field by name, preferring edit fields.
- `Scroll`: Scroll vertically or horizontally on the window or specific regions.
- `Move`: Move mouse pointer or drag (set drag=True) to coordinates. For deterministic
  drag, set `from_loc=[x, y]` with `drag=True` to press at an explicit start point and
  release at `loc` in one tool call. Optional `duration` adds bounded intermediate
  movement.
- `Shortcut`: Press keyboard shortcuts (`Ctrl+c`, `Alt+Tab`, etc).
- `Wait`: Pause for a defined duration.
- `WaitFor`: Wait until text, an active window, an element, or a focused element appears by polling UI state inside one tool call.
- `ControlStatus`: Read the desktop control owner and remaining user-idle wait. It stays available when user takeover blocks new tool calls. During AI control, a visible edge and cursor glow indicate ownership; moving the physical mouse substantially or pressing `Ctrl+Alt+Shift+Backspace` returns control to the user until 10 seconds of inactivity. During screenshot capture, the indicator is temporarily hidden and physical input is released; any physical input during this interval preempts AI control, although `ControlStatus` can briefly still report `ai` until that event is processed. Stepwise desktop input stops at its next checkpoint, but an already-running external command (for example `PowerShell` or an `App` launch) may continue after takeover; this control gate does not cancel processes or undo completed side effects. On a detected hook fault, injected keys and mouse buttons are released asynchronously, so physical input can briefly overlap a still-held AI key or button. Windows may also silently remove a low-level hook without notifying this process; periodic hook replacement reduces but does not eliminate that undetected window. User takeover is therefore best-effort, not a security boundary or a guarantee of exclusive control.
  Sessionless MCP 2026-07-28 clients cannot receive unsolicited control-state logs; they should query `ControlStatus` when they need the current owner.
- `DisplayInventory`: Read display layout, work areas, effective DPI, and scale metadata.
- `Screenshot`: Fast screenshot-first desktop capture with cursor position, active/open windows, and an image. Skips UI tree extraction for speed and should be the default first call when you mainly need visual context. Supports `display=[0]` or `display=[0,1]` using zero-based active Windows display indices, and `region=[left, top, right, bottom]` (virtual-desktop pixel coordinates) to capture just that rectangle instead of the whole screen — cheaper on tokens when you already know which area matters. `region` takes precedence over `display` when both are given; an invalid or out-of-bounds region raises an error. `save_path` (absolute file path ending in `.png`, `.jpg`/`.jpeg`, `.bmp` or `.webp`, or an absolute directory, which gets a timestamped `screenshot-*.png`) also writes the returned image to disk and reports the absolute path as `Screenshot Saved To:` in the text result; the image is still returned inline. After capture, a brief orange-red glowing border is drawn inside the captured area as a visual confirmation (set `WINDOWS_MCP_DISABLE_FLASH=1` to disable).
- `Snapshot`: Full desktop state capture for workflows that need interactive element ids, scrollable regions, or `use_dom=True` browser extraction. Supports `use_vision=True` for including screenshots, `display=[0]` or `display=[0,1]` using zero-based active Windows display indices, and `region=[left, top, right, bottom]` (virtual-desktop pixel coordinates) to inspect just that rectangle instead of the whole screen; `region` takes precedence over `display` when both are given, and an invalid or out-of-bounds region raises an error. `save_path` works as for `Screenshot` and requires `use_vision=True`.
- `App`: Launch an application by Start Menu name or strictly by executable path with separated argv and optional cwd; resize, move, and switch between windows.
- `PowerShell`: To execute PowerShell commands.
- `FileSystem`: Read, write, copy, move, delete, list, search, and inspect files and directories.
- `Scrape`: To scrape the entire webpage for information.
- `MultiSelect`: Select multiple items (files, folders, checkboxes) with optional Ctrl key. Uses bulk label-to-coordinate resolution when labels are provided.
- `MultiEdit`: Enter text into multiple input fields at specified coordinates. Uses bulk label-to-coordinate resolution when labels are provided.
- `Clipboard`: Read or set Windows clipboard content.
- `Process`: List running processes or terminate them by PID or name.
- `Notification`: Send a Windows toast notification with a title and message. Optional `app_id` (a registered AppUserModelID, e.g. `Microsoft.Windows.Explorer`) picks the app it appears under; it defaults to Windows PowerShell.
- `Registry`: Read, write, delete, or list Windows Registry values and keys.
- `Apps`: Installed-app inventory merged from Start-menu entries (AUMIDs), AppX packages, uninstall registry keys and URL protocol handlers. `mode="list"` prints one line per app (`name [id] | skill=<name>(source) | uri=<schemes> | proc=<process>`) with an optional fuzzy `query` and `limit`; `mode="info"` returns the full record plus a skill summary; `mode="refresh"` rebuilds the cached inventory and reloads skills.
- `AppSkill`: Returns the markdown skill for an app (launch commands and URI deep links, keyboard shortcuts, recipes using these tools, UI quirks) by skill name, app name, alias or process. Precedence is builtin > user > generated. `generate=true` builds a skill from the app's menu bar (`&` access keys and `Tab` accelerators), ribbon KeyTips and a bounded UI Automation walk, launching the app if needed and closing it afterwards; `regenerate=true` rebuilds a generated skill. Builtin and user skills are never overwritten. Skills are also exposed as MCP resources at `skill://<name>` (and `skill://index`). When the foreground window's app has a skill, `Snapshot` adds one line pointing to it.

### App skills

A skill is a markdown file with a small YAML frontmatter, one file per app:

```markdown
---
name: spotify
title: Spotify
description: Spotify music player; play, search, pause, skip and change volume.
version: 1
source: builtin            # builtin | user | generated
match:
  process: [spotify.exe]   # lowercase process names
  aumid: ["SpotifyAB.SpotifyMusic_*", Spotify]   # fnmatch patterns
  name: [Spotify, "Spotify *"]                   # window-title / display-name patterns
  exe: [spotify.exe]
uri_schemes: [spotify]     # without the trailing colon
---

# Spotify

## What it's for
## Launch and deep links
## Keyboard shortcuts
## Recipes
## UI quirks and accessibility notes
```

Bundled skills live in `src/windows_mcp/skills/builtin/` (File Explorer, Settings, Edge, Chrome, Notepad, Calculator, Spotify, Word, Excel, PowerPoint, Outlook, Teams, VS Code, Discord, Windows Terminal/PowerShell, Paint, Snipping Tool, Task Manager). Drop your own `.md` files into the user skill folder (`WINDOWS_MCP_SKILLS_DIR`); generated skills are written there too, with `source: generated`. The frontmatter is a deliberately small YAML subset (scalars, inline or block lists, one nested `match` map), so other tools can index or write skills without a YAML library.
- `Tasks`: Track a multi-step request as a plan of dependent tasks (`plan`, `add`, `update`, `next`, `list`, `get`, `cancel`, `clear`, `events`). Validates the dependency graph (no cycles, no unknown ids), enforces status transitions, and splits ready tasks into a sequential screen lane and a parallel background lane. Every change emits a `task_event` (MCP log notification on logger `windows-mcp.tasks`, plus an optional JSON-lines file and webhook). Never gated, so the UI keeps updating during a user takeover. See [docs/INTEGRATION.md](docs/INTEGRATION.md).
- `RunParallel`: Run several tool calls in one turn. Background calls run concurrently (bounded, per-call timeout); screen calls (`Click`, `Type`, `Scroll`, `Move`, `Shortcut`, `MultiSelect`, `MultiEdit`, `App` switch/resize) run one at a time, in order, through the screen lock. Each inner call passes the same control gate and policy as a direct call. Results come back in input order with per-call latency and error; calls with `task_id` update their task automatically.
- `Do`: Hand a small, concrete goal ("open Notepad and type a grocery list") to a fast model inside the server (default Claude Haiku 5.5, `WINDOWS_MCP_DO_MODEL`). It drives `Snapshot`, `Click`/`Type` by target name, `Shortcut`, `App`, `Scroll`, `Wait`/`WaitFor`, `PowerShell`, `AppSkill`/`Apps` and `Locate` in-process, so one planner turn does many steps. Every inner call passes the same control gate, policy and screen lock as a direct call, and `Do` holds the screen for its whole run. Returns a short transcript (steps, latency, model vs tool time) and streams `do_event`s. Needs a provider key; without one it fails with "no model configured". See [docs/INTEGRATION.md](docs/INTEGRATION.md).
- `Route`: Score text against fixed options with a decision model (default Microsoft-Decision-1 via OpenRouter, `WINDOWS_MCP_ROUTER_MODEL`) and get calibrated probabilities: route an utterance (`direct_action`, `needs_plan`, `answer_only`, `clarify`) or verify a step (`succeeded`, `failed`, `still_loading`). Needs `OPENROUTER_API_KEY` (or a chat model spec).

Screen-touching tools never interleave, even when a client sends concurrent requests: a process-wide FIFO screen lock serializes them at the tool boundary and fails with `SCREEN_BUSY` after `WINDOWS_MCP_SCREEN_LOCK_TIMEOUT` seconds.


## 🤝 Connect with Us
Stay updated and join our community:

- 📢 Follow us on [X](https://x.com/CursorTouch) for the latest news and updates

- 💬 Join our [Discord Community](https://discord.com/invite/Aue9Yj2VzS)

## Star History

[![Star History Chart](https://star-history.dera.page/svg?repos=CursorTouch/Windows-MCP&type=Date)](https://star-history.dera.page/#CursorTouch/Windows-MCP&Date)

## 👥 Contributors

Thanks to all the amazing people who have contributed to Windows-MCP! 🎉

<a href="https://github.com/CursorTouch/Windows-MCP/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=CursorTouch/Windows-MCP" />
</a>

We appreciate every contribution, whether it's code, documentation, bug reports, or feature suggestions. Want to contribute? Check out our [Contributing Guidelines](CONTRIBUTING.md)!

## 🔒 Security

**Important**: Windows-MCP operates with full system access and can perform irreversible operations. Please review our comprehensive security guidelines before deployment.

For detailed security information, including:
- Tool-specific risk assessments
- Deployment recommendations
- Vulnerability reporting procedures
- Compliance and auditing guidelines

Please read our [Security Policy](SECURITY.md).

## 📊 Telemetry

Windows-MCP collects usage data to help improve the MCP server. No personal information, no tool arguments, no outputs are tracked.

To disable telemetry, set `ANONYMIZED_TELEMETRY` to `false` in your MCP client configuration:

```json
{
  "mcpServers": {
    "windows-mcp": {
      "command": "uvx",
      "args": [
        "windows-mcp",
        "serve"
      ],
      "env": {
        "ANONYMIZED_TELEMETRY": "false"
      }
    }
  }
}
```

See the [Environment Variables](#-environment-variables) section for the full list of configurable options.

For detailed information on what data is collected and how it is handled, please refer to the [Telemetry and Data Privacy](SECURITY.md#telemetry-and-data-privacy) section in our Security Policy.

## 📝 Limitations

- Selecting specific sections of the text in a paragraph, as the MCP is relying on a11y tree. (⌛ Working on it.)
- `Type-Tool` is meant for typing text, not programming in IDE because of it types program as a whole in a file. (⌛ Working on it.)
- This MCP server can't be used to play video games 🎮.

## 🪪 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE.md) file for details.

## 🙏 Acknowledgements

Windows-MCP makes use of several excellent open-source projects that power its Windows automation features:

- [UIAutomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)

Huge thanks to the maintainers and contributors of these libraries for their outstanding work and open-source spirit.

## 🤝Contributing

Contributions are welcome! Please see [CONTRIBUTING](CONTRIBUTING.md) for setup instructions and development guidelines.

Made with ❤️ by [CursorTouch](https://github.com/CursorTouch)

## Citation

```bibtex
@software{
  author       = {CursorTouch},
  title        = {Windows-MCP: Lightweight open-source project for integrating LLM agents with Windows},
  year         = {2024},
  publisher    = {GitHub},
  url={https://github.com/CursorTouch/Windows-MCP}
}
```
