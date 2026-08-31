# vphone-mcp

MCP server for programmatic control of vphone-cli iOS VMs.

## Quick Reference

- **Install:** `uv sync`
- **Run:** `uv run vphone-mcp`
- **Test:** `uv run python -c "from vphone_mcp.client import VPhoneClient; print(VPhoneClient('/path/to/vm/vphone.sock').screenshot('/tmp/test.png'))"`
- **Env probe:** `vphone_status` tool reports CLI binary, socket, VM list, and running launches without raising.

## Architecture

```
Claude Code / Claude Desktop
    ↓ MCP (stdio)
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
2. **Screenshots & clipboard** — `screenshot` (full-res PNG + compact preview), `set_clipboard(text)` — sets the guest clipboard, NOT keyboard typing
3. **Pre-mapped navigation** — `open_app`, `tap_back`, `scroll_down`, `scroll_up`, `open_notification_center`, `open_control_center`, `open_app_switcher`, `open_search`, `swipe_to_next_page`, `swipe_to_previous_page`
4. **Raw interaction** — `tap(x, y, screen=True, delay=500)`, `swipe(x1, y1, x2, y2, duration_ms=300, screen=True, delay=500)`
5. **Environment status** — `vphone_status` (binary, socket, VM list, running launches; never raises)
6. **vphone-cli command wrappers (21)** — `vm_list`, `vm_info`, `vm_new`, `vm_config`, `vm_rename`, `vm_delete`, `vm_clone`, `vm_export`, `vm_import`, `vm_launch`, `vm_stop`, `vm_create`, `fw_catalog`, `fw_prepare`, `fw_patch`, `restore`, `cfw_install`, `patch_firmware`, `patch_component`, `setup_env`, `boot` — one tool per vphone-cli subcommand. Destructive ones (`vm_delete`, `vm_stop`, `vm_create`, `restore`, `cfw_install`) require `confirm=True` (or `force=True` for `vm_delete`); `vm_launch`/`boot` run in the background with log files under `~/.vphone/vphone-mcp/logs/`.

Every socket action also returns an inline compact grayscale screenshot; socket errors surface as clean messages, never tracebacks.

## Configuration

- `VPHONE_SOCK` — explicit hostctl socket path (auto-discovered from the VM library otherwise)
- `VPHONE_CLI_BIN` — vphone-cli binary override (default: PATH lookup, then the app bundle)
- `VPHONE_LIBRARY_ROOT` — VM library root override (default `~/.vphone/VMs`)
- `VPHONE_ROOT` — vphone project root override (default `~/.vphone`)
- `VPHONE_SUDO_PASSWORD` — sudo password fallback for `vm_create` (keeps it out of agent context)

## Coordinate System

All pixel coordinates are for the default 1290x2796 screen (3x scale). Use `screenshot()` to see the current display and derive coordinates for `tap()`.
