# vphone-mcp

MCP server for programmatic control of vphone-cli iOS VMs.

## Quick Reference

- **Install:** `uv sync`
- **Run (stdio, default):** `uv run vphone-mcp`
- **Run (remote):** `VPHONE_MCP_AUTH_TOKEN=... uv run vphone-mcp --transport http --host 0.0.0.0`
- **Run (remote, always-on):** `./contrib/launchd/install.sh` — LaunchAgent on `0.0.0.0:8765`, fixed token in `~/.vphone/mcp-auth-token` (0600), log at `~/Library/Logs/vphone-mcp.log`
- **Test:** `uv run python -c "from vphone_mcp.client import VPhoneClient; print(VPhoneClient('/path/to/vm/vphone.sock').screenshot('/tmp/test.png'))"`
- **Env probe:** `vphone_status` tool reports CLI binary, socket, VM list, and running launches without raising.

## Architecture

```
Claude Code / Claude Desktop
    ↓ MCP — stdio (local) or streamable HTTP/SSE + bearer token (remote)
vphone-mcp (Python)
    ├── Unix socket (JSON)  → vphone-cli hostctl (running VM)
    └── subprocess           → vphone-cli (VM lifecycle, firmware)
    ↓
vphone-cli (Swift, vm/vphone.sock)
    ↓ Virtualization.framework
iOS VM
```

## Tool Layers

1. **Hardware keys** — `go_home`, `press_power`, `volume_up`, `volume_down` (each takes `screen=True, delay=500`)
2. **Screenshots, clipboard & files** — `screenshot` (full-res PNG + compact preview; defaults to `$VPHONE_ROOT/vphone-mcp/screenshots/screen.png`), `set_clipboard(text)` — sets the guest clipboard, NOT keyboard typing — `get_file_url(path)`, `create_upload_url(filename)` (network transport only), `install_ipa(ipa, name=None, headless=False, variant=None)`, `list_uploads()`, `delete_upload(filename)`, `start_recording(name=None, fps=2.0, max_seconds=120, mode="auto")` / `stop_recording()` / `recording_status()`
3. **Pre-mapped navigation** — `open_app`, `tap_back`, `scroll_down`, `scroll_up`, `open_notification_center`, `open_control_center`, `open_app_switcher`, `open_search`, `swipe_to_next_page`, `swipe_to_previous_page`
4. **Raw interaction** — `tap(x, y, screen=True, delay=500)`, `swipe(x1, y1, x2, y2, duration_ms=300, screen=True, delay=500)`
5. **Environment status** — `vphone_status` (binary, socket, VM list, running launches; never raises)
6. **vphone-cli command wrappers (21)** — `vm_list`, `vm_info`, `vm_new`, `vm_config`, `vm_rename`, `vm_delete`, `vm_clone`, `vm_export`, `vm_import`, `vm_launch`, `vm_stop`, `vm_create`, `fw_catalog`, `fw_prepare`, `fw_patch`, `restore`, `cfw_install`, `patch_firmware`, `patch_component`, `setup_env`, `boot` — one tool per vphone-cli subcommand. Destructive ones (`vm_delete`, `vm_stop`, `vm_create`, `restore`, `cfw_install`) require `confirm=True` (or `force=True` for `vm_delete`); `vm_launch`/`boot` run in the background with log files under `$VPHONE_ROOT/vphone-mcp/logs/`.

Every socket action also returns an inline compact grayscale screenshot; socket errors surface as clean messages, never tracebacks.

## Transports and remote file access

`main()` defers to `remote.parse_config()` / `remote.run_server()`. stdio stays
the default; `--transport http` (or `sse`) serves the same tool set over the
network, with the whole ASGI app wrapped in `remote.AuthMiddleware`
(constant-time compare, `GET /healthz` exempt). A network transport refuses to
start without `VPHONE_MCP_AUTH_TOKEN` unless `--allow-anonymous` is passed —
the tools launch VMs and run sudo, so the token is the only gate. The token is
env-only by design (never argv). Binding a non-loopback host replaces FastMCP's
import-time localhost DNS-rebinding allowlist, which would otherwise 421 every
real Host header.

`vphone_mcp/files.py` makes server-side files fetchable by remote clients over
`GET /files/<path under files.root()>` (e.g. `/files/screenshots/screen.png`).
Auth is HTTP Basic (the only scheme that fits inside a URL) but introduces no
second secret: the password is the MCP token, the user is `sha256(<resolved
absolute path>)`. Serving is stateless — `files.resolve()` joins the relative
path onto the root, normalises it, and returns a file only when it stayed
inside the root, is the spelling `relative_path()` would mint (so each file has
exactly one URL — a `..` detour is a 404), hashes to the presented user, and is
a regular file. Traversal, absolute paths and symlinks out of scope therefore
fail without ever being opened, and links survive a restart. `files.root()` is
`$VPHONE_ROOT/vphone-mcp` and nothing else is served: the VM library and the
rest of the vphone root stay unreachable. `files.url_for()` returns None on
stdio, so tools silently skip the link when it is meaningless.
`cli.run_background()` and `server._guest_network()` take the log directory
from `files.log_dir()`, so a custom `$VPHONE_ROOT` keeps launch logs in scope.

`files.upload_dir()` (`<root>/uploads`) is the one writable path: the same
endpoint takes PUT there, verified by `files.upload_target()` (canonical path,
hash match, parent must be the upload dir, no existence requirement), streamed
through a `.part` file and capped by `$VPHONE_MCP_MAX_UPLOAD_MB`. That is how a
remote client gets an IPA onto the host — `create_upload_url` mints the PUT URL,
`install_ipa` then runs `boot --config <vm>/config.plist --install-ipa <file>`
detached. vphone-cli has no install-into-a-running-VM path, so `install_ipa`
boots the VM and refuses while `_running_launch_pids()` is non-empty. Nothing
else deletes uploads, so `files.prune_uploads()` runs before each new upload and
on each listing. It judges age purely from each entry's mtime — no state between
requests — against `$VPHONE_MCP_UPLOAD_TTL_HOURS` (default 3h; stale `.part`
files go after 1h regardless). `list_uploads()` / `delete_upload()` expose the
directory to the agent.

## Recording

Two mechanisms in `vphone_mcp/recording.py`, selected by `mode`
(`auto` → native, falling back to poll and saying why).

**native** — vphone-cli's own `VPhoneScreenRecorder`
(`AVAssetWriterInputPixelBufferAdaptor` fed from the VM view in-process): much
cheaper than polling, but it is not on the hostctl socket (probed: `record`,
`recording`, `startRecording`, `toggleRecording` all answer `unknown command`),
so `vphone_mcp/menu.py` clicks its menu item through System Events. That needs
the Accessibility permission. The item's title is NOT hardcoded — Swift stores
strings ≤15 bytes inline, so it is absent from the binary; the menus are read at
runtime and matched on `$VPHONE_MCP_RECORD_MENU_KEYWORDS` (default "record"),
and a failed match lists every item. Stopping reads the app's saved-file dialog
for the path (and must dismiss it — it blocks the UI), else takes the newest
`vphone-recording-*` under `search_dirs()`. The file is hard-linked (copy as
fallback) into `files.recording_dir()` rather than moved, so vphone-cli's
"Reveal in Finder" still works.

**poll** — the no-permission fallback. A daemon thread asks for a screenshot
every `1/fps` seconds and pipes the PNG into `ffmpeg -f image2pipe`, so only one
temp frame ever sits on disk (a frame is ~3MB; a capture round-trip is ~90ms,
hence `MAX_FPS = 5`). ffmpeg's stderr goes to a file, never a pipe, so it cannot
block while being fed. Without ffmpeg the frames are kept and zipped at stop.
`max_seconds` ends capture but `stop_recording()` is what finalises the file.

One recording at a time (module lock). Output in `files.recording_dir()` is
fetchable over the file endpoint and kept until deleted; setting
`$VPHONE_MCP_RECORDING_TTL_HOURS` prunes older ones when a new recording starts
(off by default — recordings are usually evidence).

## Configuration

- `VPHONE_SOCK` — explicit hostctl socket path (auto-discovered from the VM library otherwise)
- `VPHONE_CLI_BIN` — vphone-cli binary override (default: PATH lookup, then the app bundle)
- `VPHONE_LIBRARY_ROOT` — VM library root override (default: vphone-cli's `~/.vphone/machines`, then legacy `~/.vphone/VMs`; see `files.library_roots()`)
- `VPHONE_ROOT` — vphone project root override (default `~/.vphone`)
- `VPHONE_SUDO_PASSWORD` — sudo password fallback for `vm_create` (keeps it out of agent context)
- `VPHONE_MCP_AUTH_TOKEN` / `VPHONE_MCP_AUTH_TOKEN_FILE` — bearer token for network transports
- `VPHONE_MCP_MAX_UPLOAD_MB` — upload size cap (default 4096, `0` disables)
- `VPHONE_MCP_UPLOAD_TTL_HOURS` — how long staged uploads are kept (default 3, `0` keeps them)
- `VPHONE_MCP_RECORDING_TTL_HOURS` — prune recordings older than this many hours (default `0` = keep them)
- `VPHONE_MCP_FFMPEG_BIN` — ffmpeg override for poll mode (default: PATH; absent → zipped PNG frames)
- `VPHONE_MCP_APP_PROCESS`, `VPHONE_MCP_RECORD_MENU_KEYWORDS`, `VPHONE_MCP_RECORDING_SEARCH_DIRS` — native-mode menu driving and artifact discovery
- `VPHONE_MCP_TRANSPORT`, `_HOST`, `_PORT`, `_PATH`, `_ALLOW_ANONYMOUS`, `_JSON_RESPONSE`, `_STATELESS`, `_ALLOWED_HOSTS`, `_ALLOWED_ORIGINS`, `_SSL_CERTFILE`, `_SSL_KEYFILE`, `_PUBLIC_URL`, `_DISABLE_FILES` — transport settings, each with a matching CLI flag (`vphone-mcp --help`)

## Coordinate System

All pixel coordinates are for the default 1290x2796 screen (3x scale). Use `screenshot()` to see the current display and derive coordinates for `tap()`.
