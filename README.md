# vphone-mcp

MCP server for programmatic control of [vphone-cli](https://github.com/Lakr233/vphone-cli) iOS VMs. Enables AI-driven E2E testing by exposing the VM's display, touch input, navigation, and the full vphone-cli command surface as MCP tools.

## How it works

```
Claude Code / Claude Desktop          (local: stdio · remote: HTTP + bearer token)
    │ MCP
    ▼
vphone-mcp (Python)
    ├── Unix socket (JSON)  → vphone-cli hostctl (running VM)
    └── subprocess           → vphone-cli (VM lifecycle, firmware)
    ▼
vphone-cli (Swift, vm/vphone.sock)
    │ Virtualization.framework
    ▼
iOS 26 VM
```

Every socket action returns a compact grayscale screenshot (~20-30KB) inline in the response, so the LLM can see what happened without a separate call. The 21 CLI wrapper tools forward vphone-cli's stdout/stderr faithfully.

## Setup

Requires [uv](https://github.com/astral-sh/uv) and a running vphone-cli VM with the host control socket enabled (PR [#261](https://github.com/Lakr233/vphone-cli/pull/261)).

```bash
git clone https://github.com/pluginslab/vphone-mcp.git
cd vphone-mcp
uv sync
```

### Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "vphone": {
      "command": "uv",
      "args": ["--directory", "/path/to/vphone-mcp", "run", "vphone-mcp"],
      "env": {
        "VPHONE_SOCK": "/path/to/vphone-cli/vm/vphone.sock"
      }
    }
  }
}
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "vphone": {
      "command": "uv",
      "args": ["--directory", "/path/to/vphone-mcp", "run", "vphone-mcp"],
      "env": {
        "VPHONE_SOCK": "/path/to/vphone-cli/vm/vphone.sock"
      }
    }
  }
}
```

### Remote (HTTP) transport

The VMs only run on the Mac that hosts vphone-cli, but the agent does not have
to. Run the server there with a network transport and point remote clients at
it. A static bearer token read from the environment guards every endpoint — the
token never goes on the command line, so it stays out of `ps` output.

On the VM host:

```bash
export VPHONE_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"   # keep this value
uv run vphone-mcp --transport http --host 0.0.0.0 --port 8765
```

From anywhere that can reach it:

```bash
claude mcp add --transport http vphone https://vphone.example.com/mcp \
  --header "Authorization: Bearer $VPHONE_MCP_AUTH_TOKEN"
```

Or in a client config file:

```json
{
  "mcpServers": {
    "vphone": {
      "type": "http",
      "url": "https://vphone.example.com/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Notes:

- **Authentication is mandatory** for network transports: the tools launch VMs
  and run vphone-cli with sudo, so the server refuses to start without
  `VPHONE_MCP_AUTH_TOKEN` (or `VPHONE_MCP_AUTH_TOKEN_FILE`). `--allow-anonymous`
  opts out explicitly, for when a tunnel or authenticating proxy already gates
  access.
- **Use TLS.** A bearer token over plain HTTP is a token in cleartext. Put the
  server behind a TLS-terminating reverse proxy or tunnel (Cloudflare Tunnel,
  Tailscale, nginx), or serve HTTPS directly with `--ssl-certfile`/`--ssl-keyfile`.
  The server prints a warning when it binds a non-loopback address without TLS.
- `GET /healthz` answers `{"status":"ok"}` without a token, for supervisors and
  proxy health checks. Nothing else is reachable unauthenticated.
- `--transport sse` serves the legacy SSE transport (`/sse` + `/messages/`) for
  older clients; both endpoints require the same token.
- Binding a non-loopback address disables the SDK's DNS-rebinding host allowlist
  (the token is the gate). Pass `--allowed-host vphone.example.com` to turn it
  back on for a known hostname.
- Paths in tool arguments (e.g. `screenshot(save_path=...)`, `vm_export(out=...)`)
  resolve on the **server** host, not the client's — see file access below.

#### Run it at login (launchd, macOS)

`contrib/launchd/install.sh` installs a LaunchAgent that serves
`0.0.0.0:8765` at login with one fixed token, and prints the client config:

```bash
./contrib/launchd/install.sh
```

The token is generated once into `~/.vphone/mcp-auth-token` (0600) and reused by
later re-installs, so configured clients keep working; the plist points
`VPHONE_MCP_AUTH_TOKEN_FILE` at it rather than carrying the value. Host, port,
public URL and the token itself are overridable from the environment — see
[contrib/launchd/README.md](contrib/launchd/README.md).

#### File access

Tools name files on the server's disk: the full-resolution screenshot, the
launch log, an exported `.vphone` archive. A remote client cannot read any of
them, so the server also exposes `GET /files/<path>`, where `<path>` is relative
to a vphone root:

```
http://<hash>:<mcp-token>@host:8765/files/screenshots/screen.png
       └ sha256 of the resolved absolute path
               └ the same token the MCP endpoint uses
```

- `screenshot()` and the background launch tools (`vm_launch`, `boot`) include a
  URL in their result; `get_file_url(path)` mints one for any other server-side
  path. `curl -O '<url>'` works as-is — HTTP Basic is used precisely because the
  credentials fit inside the URL.
- **No extra secret and no server state.** The Basic password is the MCP token
  you already configured, and the user is the hash that pins the link to one
  file. Serving a request means joining the relative path onto the root,
  normalising it, and answering only when the result stays inside that root
  **and** hashes to what the request presented. A tampered path — `../`, an
  absolute path, a different file — resolves elsewhere, so its hash no longer
  matches and it never reaches the filesystem. Links stay valid across restarts.
- Exactly one directory is served: `$VPHONE_ROOT/vphone-mcp` (default
  `~/.vphone/vphone-mcp`), which is this server's own — `logs/` and
  `screenshots/`. The VM library and the rest of the vphone root stay
  unreachable, so write anything a client must fetch there (e.g.
  `vm_export(out=...)`). Each file has exactly one URL: a `..` detour that
  normalises to the same file is a 404, and paths are resolved before the scope
  check, so a symlink inside the root pointing outside it is rejected.
- Set `VPHONE_MCP_PUBLIC_URL` when the server sits behind a proxy or binds
  `0.0.0.0`, otherwise the emitted links point at the bind address. Serve the
  whole thing over TLS: these URLs carry the token.
- `--no-files` turns the endpoint off entirely.

#### Installing an IPA from a remote client

`uploads/` is the one writable directory, so a client can put an app on the VM
host and install it:

```
create_upload_url("MyApp.ipa")   → curl -T MyApp.ipa '<url>'   → install_ipa("MyApp.ipa")
```

`create_upload_url` mints a PUT URL (same auth as a download) and prints the
curl line; the body is streamed to `$VPHONE_ROOT/vphone-mcp/uploads/MyApp.ipa`
through a `.part` file, so a truncated transfer never looks like a finished
IPA. `VPHONE_MCP_MAX_UPLOAD_MB` caps the size (default 4096).

`install_ipa` then runs `vphone-cli boot --config <vm>/config.plist
--install-ipa <file>` detached, like `vm_launch`, and returns the pid, log path
and log URL. vphone-cli installs only at boot, so **this boots the VM** — stop a
running one with `vm_stop` first (the tool refuses while a VM process is up).
Pass `name=` when the library holds more than one VM.

An upload is staging for one install, not storage, so it is swept quickly:
anything older than `VPHONE_MCP_UPLOAD_TTL_HOURS` (default **3 hours**, `0`
keeps them) goes, as does any `.part` left by an interrupted transfer after an
hour. Age is read from each file's mtime, so nothing is tracked between requests
and a restart changes nothing. The sweep runs before each new upload and on
`list_uploads()`; `delete_upload(name)` reclaims the space immediately — worth
doing after an install, since IPAs are large.

## Tools

### Hardware Keys
| Tool | Description |
|------|-------------|
| `go_home(screen=True, delay=500)` | Press home button |
| `press_power(screen=True, delay=500)` | Lock/wake the screen |
| `volume_up(screen=True, delay=500)` | Volume up |
| `volume_down(screen=True, delay=500)` | Volume down |

### Screenshots, Clipboard & Files
| Tool | Description |
|------|-------------|
| `screenshot(save_path=None, include_full_png=False)` | Full-res PNG + compact preview (returns embedded images); saves to `$VPHONE_ROOT/vphone-mcp/screenshots/screen.png` by default |
| `set_clipboard(text, screen=True, delay=500)` | Set the guest clipboard (NOT keyboard typing) |
| `start_recording(name=None, fps=2.0, max_seconds=120, mode="auto")` | Start recording the display (vphone-cli's own recorder, or polling) |
| `stop_recording()` | Finish the recording; returns path, frame count and a URL |
| `recording_status()` | Whether a recording is running, and how many frames so far |
| `get_file_url(path)` | URL for a server-side file, for remote clients (network transport only — see [File access](#file-access)) |
| `create_upload_url(filename)` | URL a remote client can PUT a file (e.g. an IPA) to (network transport only) |
| `install_ipa(ipa, name=None, headless=False, variant=None)` | Boot a VM with an IPA/TIPA installed once the guest channel connects |
| `list_uploads()` | Staged uploads with size and age |
| `delete_upload(filename)` | Remove one staged upload |

### Pre-mapped Navigation
| Tool | Description |
|------|-------------|
| `open_app(name)` | Open an app by name from the home screen |
| `tap_back` | Tap the iOS back button (top-left) |
| `scroll_down` | Scroll down on current screen |
| `scroll_up` | Scroll up on current screen |
| `open_notification_center` | Swipe down from top-left |
| `open_control_center` | Swipe down from top-right |
| `open_app_switcher` | Slow swipe up from bottom |
| `open_search` | Tap the home screen Search bar |
| `swipe_to_next_page` | Swipe to next home screen page |
| `swipe_to_previous_page` | Swipe to previous home screen page |

Supported app names for `open_app`: FaceTime, Calendar, Photos, Mail, Notes, Reminders, Clock, TV, Games, App Store, Maps, Health, Wallet, Settings, Phone, Safari, Messages, Music.

### Recording

Two mechanisms, chosen with `mode` (default `auto` = native, falling back to
poll). Interaction tools keep working while either runs:

```
start_recording(name="login-flow") → open_app("Settings") → tap(...) → stop_recording()
```

**`native` — vphone-cli's own recorder.** It feeds
`AVAssetWriterInputPixelBufferAdaptor` straight from the VM view in-process: no
PNG per frame, no IPC, no ffmpeg, so it is by far the cheaper of the two. It is
not on the host control socket (`{"t":"record"}` answers `unknown command`), so
the only handle on it is the menu item `VPhoneMenuRecord.swift` installs, which
means macOS **Accessibility permission**, granted to whatever runs this server
(System Settings → Privacy & Security → Accessibility). No script can grant it
— the entry lives in the SIP-protected system TCC database and `tccutil` only
resets — so `contrib/launchd/install.sh` instead reports whether the running
agent has it and names the exact binary to add (`--open-accessibility` opens the
pane and reveals it in Finder). `vphone_status` reports the same thing.
Nothing hardcodes the item's title — Swift stores short strings inline, so the
menus are read at runtime and matched on "record"; if the match fails the error
lists every menu item, and `VPHONE_MCP_RECORD_MENU_KEYWORDS` overrides the word.
vphone-cli picks the frame rate, file name and destination, so `name`, `fps` and
`max_seconds` do not apply. `stop_recording()` reads the path out of the app's
"saved" dialog (and dismisses it, since it blocks the UI), falling back to the
newest `vphone-recording-*` under `VPHONE_MCP_RECORDING_SEARCH_DIRS`. The file
stays where vphone-cli put it and is hard-linked into the served directory, so
"Reveal in Finder" keeps working and no space is used twice.

**`poll` — this server's fallback, no permission needed.** It asks the socket
for a screenshot every `1/fps` seconds and pipes each full-resolution PNG into
ffmpeg — one temp frame on disk instead of gigabytes of PNGs. A capture
round-trip is ~90 ms, so `fps` is capped at 5; 2 is plenty for UI evidence.
`max_seconds` (default 120) is a hard stop so a forgotten recording cannot fill
the disk — capture ends there and `stop_recording()` still finalises the file.
Without ffmpeg the frames are kept and zipped instead, so there is always
exactly one artifact (much larger: a 6-frame zip is ~19 MB where a 13-frame
H.264 file is ~0.5 MB).

Either way `stop_recording()` returns the server-side path and a URL to fetch it
with. Recordings are kept until deleted; set `VPHONE_MCP_RECORDING_TTL_HOURS`
to sweep older ones whenever a new recording starts. One recording at a time.

### Raw Interaction
| Tool | Description |
|------|-------------|
| `tap(x, y, screen=True, delay=500)` | Tap at pixel coordinates (1290x2796) |
| `swipe(x1, y1, x2, y2, duration_ms=300, screen=True, delay=500)` | Swipe between two points |

On all socket tools, `screen` controls the inline post-action screenshot (`screen=False` skips the capture) and `delay` is the settle time in ms before it. Every socket tool returns the status text plus the inline compact screenshot; when no VM is running they return a clean error message instead of a traceback.

### Environment Status
| Tool | Description |
|------|-------------|
| `vphone_status` | CLI binary path + PATH presence, resolved socket path + existence, `vm list --json` summary (parsed), running VM (vphone-cli / vphone-vm) PIDs. Never raises. |

### vphone-cli Command Wrappers (20 tools)
| Tool | Description |
|------|-------------|
| `vm_list(json=False, library_root=None)` | List VM bundles (machine-readable with `json=True`) |
| `vm_info(name=None, json=False, library_root=None)` | Show a VM's configuration details |
| `vm_new(name, cpu=None, memory=None, disk_size=None, rom=None, seprom=None, library_root=None)` | Create an empty VM bundle |
| `vm_config(name=None, cpu=None, memory=None, network=None, bridge_interface=None, library_root=None)` | Adjust a VM's CPU/memory/network config |
| `vm_rename(name=None, new_name=None, library_root=None)` | Rename a VM (must be stopped) |
| `vm_delete(name=None, force=False, library_root=None, confirm=False)` | Delete a VM; `confirm=True` required unless `force=True` |
| `vm_clone(name=None, new_name=None, library_root=None)` | Clone a VM — the clone gets a fresh device identity, ideal for per-testcase VMs |
| `vm_export(name=None, out=<required>, max_=False, include_ipsw=False, library_root=None)` | Export a VM to a `.vphone` archive |
| `vm_import(input=<required>, name=None, library_root=None)` | Import a `.vphone` archive as a new VM |
| `vm_launch(name=None, dfu=False, headless=False, api_listen=None, kernel_debug_port=None, project_root=None, library_root=None, verbose=0)` | Launch a VM — blocking process, runs in background with a log file |
| `vm_stop(name=None, timeout=None, library_root=None, confirm=False)` | Stop a running VM (SIGINT then SIGKILL) |
| `vm_create(name=<required>, iphone_source=None, cloudos_source=None, gpu_driver_bundle=None, ipsw_cache=None, device=None, disk_size=None, preset=None, keep_artifacts=False, verbose=0, library_root=None, confirm=False)` | End-to-end create pipeline (prepare→patch→restore→CFW→boot); needs internet + sudo, long-running |
| `fw_catalog(json=False)` | List firmware known to the cache |
| `fw_prepare(name=None, iphone_source=None, cloudos_source=None, iphone_version=None, iphone_build=None, list_=False, project_root=None, library_root=None, verbose=0)` | Prepare firmware (downloads IPSW/cloudOS, staged in cache) |
| `fw_patch(name=None, preset=None, quiet=False, library_root=None)` | Patch the boot chain with a patch preset |
| `restore(name=None, get_shsh=False, offline=False, udid=None, ecid=None, library_root=None, verbose=0, confirm=False)` | Restore a VM from prepared firmware |
| `cfw_install(name=None, keep_artifacts=False, library_root=None, confirm=False)` | Install custom firmware via host mount; runs under sudo |
| `patch_firmware(vm_directory=<required>, records_out=None, quiet=False, preset=None)` | Patch a VM directory's boot-chain firmware |
| `patch_component(component=<required>, input=<required>, output=<required>, quiet=False, records_out=None, target_os=None, frida=False)` | Patch a single firmware component (txm/kernel-base/kernel-jb) |
| `boot(config=<required>, dfu=False, headless=False, api_listen=None, kernel_debug_port=None, vphoned_bin=None, install_ipa=None)` | Boot from a raw config.plist manifest — blocking process, runs in background with a log file |

Each wrapper maps 1:1 onto a vphone-cli subcommand (flags use canonical long forms, e.g. `-d/--dfu` is passed as `--dfu`) and reports exit code, argv, stdout, and stderr — a nonzero exit is returned as text for the LLM to read, not an exception. Destructive/privileged tools (`vm_delete`, `vm_stop`, `vm_create`, `restore`, `cfw_install`) require `confirm=True`; `vm_delete` also accepts `force=True` to skip the gate (mirrors the CLI's `-f`). Long-running pipeline tools have generous timeouts and only raise on timeout. `vm_launch` and `boot` are blocking VM processes that run detached, logging to `$VPHONE_ROOT/vphone-mcp/logs/<name>_launch_<epoch>.log`. Privileged commands (`vm_create`, `cfw_install`) run under `sudo` — set `VPHONE_SUDO_PASSWORD` for non-interactive elevation, since the server has no TTY.

### Not exposed (hostctl socket limitation)

The hostctl socket only implements the 5 commands above — there is no guest control channel for: files, apps, keychain, settings, accessibility tree, and clipboard-get (reading the guest clipboard). These would require an upstream vphone-cli extension and are out of scope.

## Example session

```
User: Open Settings and navigate to General > About

Claude: [calls open_app("Settings")]
        → sees Settings list
        [calls tap(400, 1880)]
        → sees General page
        [calls tap(400, 1100)]
        → sees About page with iOS 26.1, Serial: vphone-1337
```

## Management CLI

`vphone-mcp` doubles as a host-management tool. With no arguments (or `serve`)
it runs the MCP server exactly as before; these subcommands reconfigure the
host and keep the AMFI allowlist in shape:

```
vphone-mcp versions            # list installed vphone-cli versions
vphone-mcp select 2.6.0        # pin the version the server drives
vphone-mcp doctor              # probe env + validate the registry vs the binary
vphone-mcp escalator status    # AMFI code-requirement allowlist state (needs root)
vphone-mcp escalator allow     # allow vphone-vm, the companion VM binary (needs root)
```

- `versions` auto-detects installed launchpad bundles and marks the active one.
- `select <version>` persists the choice to `~/.vphone/config.json` (or
  `$VPHONE_MCP_CONFIG`); the server resolves it on next start.
- `doctor` is the drift backstop: it runs every registry subcommand's `--help`
  against the live binary and reports any missing subcommand or flag, so a
  vphone-cli update surfaces as a concrete list instead of a runtime error.
- `escalator allow` writes the `vphone-vm` cdhash into the AMFI
  code-requirement allowlist — the same `sudo vphone-escalator allow …` you
  would otherwise run by hand. `VPHONE_SUDO_PASSWORD` makes it non-interactive.

## Configuration

| Env var | Description |
|---------|-------------|
| `VPHONE_SOCK` | Path to `vphone.sock` (auto-discovered if not set) |
| `VPHONE_CLI_BIN` | Explicit path to the vphone-cli binary (overrides version selection and auto-detect) |
| `VPHONE_CLI_VERSION` | Pin a specific installed vphone-cli version, e.g. `2.6.0` (same as `vphone-mcp select`; persisted in `~/.vphone/config.json`) |
| `VPHONE_MCP_CONFIG` | Config file holding the selected version (default `~/.vphone/config.json`) |
| `VPHONE_LIBRARY_ROOT` | VM library root override (default: vphone-cli's `~/.vphone/machines`, then legacy `~/.vphone/VMs`) |
| `VPHONE_ROOT` | vphone project root override (default `~/.vphone`) |
| `VPHONE_SUDO_PASSWORD` | sudo password for privileged commands (`vm_create`, `cfw_install`, `escalator allow`) — drives `sudo -A` non-interactively; never logged |

### Transport / remote access

Every setting below has a matching CLI flag (`vphone-mcp --help`); flags win over env vars.

| Env var | Flag | Description |
|---------|------|-------------|
| `VPHONE_MCP_TRANSPORT` | `--transport` | `stdio` (default), `streamable-http` (alias `http`), or `sse` |
| `VPHONE_MCP_AUTH_TOKEN` | — | Bearer token required by network transports. Deliberately env-only so it never appears in argv |
| `VPHONE_MCP_AUTH_TOKEN_FILE` | — | File to read the token from when `VPHONE_MCP_AUTH_TOKEN` is unset (launchd/docker secrets) |
| `VPHONE_MCP_HOST` | `--host` | Bind address (default `127.0.0.1`; `0.0.0.0` to accept remote clients) |
| `VPHONE_MCP_PORT` | `--port` | Bind port (default `8765`) |
| `VPHONE_MCP_PATH` | `--path` | Streamable-HTTP endpoint path (default `/mcp`) |
| `VPHONE_MCP_ALLOW_ANONYMOUS` | `--allow-anonymous` | Serve without a token — only when something else gates access |
| `VPHONE_MCP_JSON_RESPONSE` | `--json-response` | Plain JSON replies instead of SSE streams (for SSE-buffering proxies) |
| `VPHONE_MCP_STATELESS` | `--stateless` | New transport per request — no session affinity, for load-balanced setups |
| `VPHONE_MCP_ALLOWED_HOSTS` | `--allowed-host` | Comma-separated Host allowlist (re-enables DNS-rebinding protection) |
| `VPHONE_MCP_ALLOWED_ORIGINS` | `--allowed-origin` | Comma-separated Origin allowlist |
| `VPHONE_MCP_SSL_CERTFILE` | `--ssl-certfile` | TLS certificate for direct HTTPS serving |
| `VPHONE_MCP_SSL_KEYFILE` | `--ssl-keyfile` | TLS private key for direct HTTPS serving |
| `VPHONE_MCP_PUBLIC_URL` | `--public-url` | External base URL used to build file links (set it behind a proxy or a wildcard bind) |
| `VPHONE_MCP_DISABLE_FILES` | `--no-files` | Do not serve `/files/<path>` at all |
| `VPHONE_MCP_MAX_UPLOAD_MB` | — | Upload size cap in MB (default 4096; `0` disables the cap) |
| `VPHONE_MCP_UPLOAD_TTL_HOURS` | — | How long staged uploads are kept (default 3; `0` keeps them until deleted) |
| `VPHONE_MCP_RECORDING_TTL_HOURS` | — | Prune recordings older than this many hours when a new one starts (default `0` = keep until deleted) |
| `VPHONE_MCP_FFMPEG_BIN` | — | ffmpeg binary override for poll mode (default: PATH lookup; without it recordings are zipped PNG frames) |
| `VPHONE_MCP_APP_PROCESS` | — | vphone-cli's UI process name for menu driving (default `vphone-cli`) |
| `VPHONE_MCP_RECORD_MENU_KEYWORDS` | — | Comma-separated words matching the recording menu item (default `record`) |
| `VPHONE_MCP_RECORDING_SEARCH_DIRS` | — | Where to look for a native recording's file (default: Movies, Desktop, Downloads, Pictures, `$VPHONE_ROOT`, the VM library, tmp) |

## License

MIT
