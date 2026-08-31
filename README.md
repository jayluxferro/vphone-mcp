# vphone-mcp

MCP server for programmatic control of [vphone-cli](https://github.com/Lakr233/vphone-cli) iOS VMs. Enables AI-driven E2E testing by exposing the VM's display, touch input, navigation, and the full vphone-cli command surface as MCP tools.

## How it works

```
Claude Code / Claude Desktop
    │ MCP (stdio)
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

## Tools

### Hardware Keys
| Tool | Description |
|------|-------------|
| `go_home(screen=True, delay=500)` | Press home button |
| `press_power(screen=True, delay=500)` | Lock/wake the screen |
| `volume_up(screen=True, delay=500)` | Volume up |
| `volume_down(screen=True, delay=500)` | Volume down |

### Screenshots & Clipboard
| Tool | Description |
|------|-------------|
| `screenshot` | Full-res PNG + compact preview (returns embedded images) |
| `set_clipboard(text, screen=True, delay=500)` | Set the guest clipboard (NOT keyboard typing) |

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

### Raw Interaction
| Tool | Description |
|------|-------------|
| `tap(x, y, screen=True, delay=500)` | Tap at pixel coordinates (1290x2796) |
| `swipe(x1, y1, x2, y2, duration_ms=300, screen=True, delay=500)` | Swipe between two points |

On all socket tools, `screen` controls the inline post-action screenshot (`screen=False` skips the capture) and `delay` is the settle time in ms before it. Every socket tool returns the status text plus the inline compact screenshot; when no VM is running they return a clean error message instead of a traceback.

### Environment Status
| Tool | Description |
|------|-------------|
| `vphone_status` | CLI binary path + PATH presence, resolved socket path + existence, `vm list --json` summary (parsed), running vphone-cli launch PIDs. Never raises. |

### vphone-cli Command Wrappers (21 tools)
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
| `vm_launch(name=None, dfu=False, headless=False, variant=None, no_vphoned=False, kernel_debug_port=None, project_root=None, library_root=None, verbose=0)` | Launch a VM — blocking process, runs in background with a log file |
| `vm_stop(name=None, timeout=None, library_root=None, confirm=False)` | Stop a running VM (SIGINT then SIGKILL) |
| `vm_create(name=<required>, variant=None, iphone_source=None, cloudos_source=None, disk_size=None, sudo_password=None, spoof_build=None, force_dsc_maxslide=False, frida=False, root_popup=False, interactive=False, keep_artifacts=False, project_root=None, verbose=0, library_root=None, confirm=False)` | End-to-end create pipeline (prepare→patch→restore→CFW→boot); needs internet + sudo, long-running |
| `fw_catalog(json=False)` | List firmware known to the cache |
| `fw_prepare(name=None, iphone_source=None, cloudos_source=None, iphone_version=None, iphone_build=None, list_=False, project_root=None, library_root=None, verbose=0)` | Prepare firmware (downloads IPSW/cloudOS, staged in cache) |
| `fw_patch(name=None, variant=None, force_exc_guard=False, frida=False, quiet=False, library_root=None)` | Patch a prepared firmware set |
| `restore(name=None, get_shsh=False, offline=False, udid=None, ecid=None, project_root=None, library_root=None, verbose=0, confirm=False)` | Restore a VM from prepared firmware |
| `cfw_install(name=None, variant='exp', spoof_build=None, force_dsc_maxslide=False, use_sudo=False, keep_artifacts=False, project_root=None, library_root=None, verbose=0, confirm=False)` | Install custom firmware; uses `--root-popup` by default, `use_sudo=True` for the plain sudo re-exec |
| `patch_firmware(vm_directory=<required>, variant=None, records_out=None, quiet=False, no_binpack=False, no_vphoned=False, force_exc_guard=False, frida=False)` | Patch a VM directory's firmware in place |
| `patch_component(component=<required>, input=<required>, output=<required>, quiet=False, records_out=None, target_os=None, frida=False)` | Patch a single firmware component (txm/kernel-base/kernel-jb) |
| `setup_env(force=False, project_root=None)` | Provision the vphone Python environment (~/.vphone/venv) |
| `boot(config=<required>, dfu=False, headless=False, kernel_debug_port=None, vphoned_bin=None, variant=None, install_ipa=None, no_vphoned=False)` | Boot from a raw config.plist manifest — blocking process, runs in background with a log file |

Each wrapper maps 1:1 onto a vphone-cli subcommand (flags use canonical long forms, e.g. `-d/--dfu` is passed as `--dfu`) and reports exit code, argv, stdout, and stderr — a nonzero exit is returned as text for the LLM to read, not an exception. Destructive/privileged tools (`vm_delete`, `vm_stop`, `vm_create`, `restore`, `cfw_install`) require `confirm=True`; `vm_delete` also accepts `force=True` to skip the gate (mirrors the CLI's `-f`). Long-running pipeline tools have generous timeouts and only raise on timeout. `vm_launch` and `boot` are blocking VM processes that run detached, logging to `~/.vphone/vphone-mcp/logs/<name>_launch_<epoch>.log`; `sudo_password` for `vm_create` falls back to `VPHONE_SUDO_PASSWORD` so it can stay out of agent context.

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

## Configuration

| Env var | Description |
|---------|-------------|
| `VPHONE_SOCK` | Path to `vphone.sock` (auto-discovered if not set) |
| `VPHONE_CLI_BIN` | Path to the vphone-cli binary (default: PATH lookup, then /Applications/vphone-cli.app) |
| `VPHONE_LIBRARY_ROOT` | VM library root override (default `~/.vphone/VMs`) |
| `VPHONE_ROOT` | vphone project root override (default `~/.vphone`) |
| `VPHONE_SUDO_PASSWORD` | sudo password fallback for `vm_create` — keeps it out of agent transcripts |

## License

MIT
