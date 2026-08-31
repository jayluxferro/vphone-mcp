# SPEC: Full vphone-cli coverage in vphone-mcp

## Objectives

Make every programmatically reachable vphone-cli 1.0.12 feature available as an MCP tool:

1. **Socket layer (Layer 1)** — complete the 5-command hostctl protocol, expose
   `screen`/`delay` options, surface the inline post-action image, fix socket discovery.
2. **CLI layer (Layer 3)** — wrap all vphone-cli subcommands as MCP tools via a
   data-driven registry (subprocess execution).
3. Layer 2 (guest control channel) is out of scope — the hostctl socket does not
   expose it and the MCP cannot reach vphoned directly. Documented, not implemented.

Success metric: `uv run vphone-mcp` starts and lists ~40 tools; every socket tool
returns a clean structured error (not a crash) when no VM is running; every CLI
wrapper tool forwards vphone-cli output faithfully; smoke tests pass.

## Verified environment facts (do not re-derive)

- vphone-cli 1.0.12 installed at `/opt/homebrew/bin/vphone-cli`
  (also `/Applications/vphone-cli.app/Contents/MacOS/vphone-cli`).
- Hostctl socket: `<VM bundle dir>/vphone.sock` = `~/.vphone/VMs/<name>/vphone.sock`
  (socket path = config.plist's directory + "vphone.sock"). `$VPHONE_LIBRARY_ROOT`
  overrides `~/.vphone/VMs`. `$VPHONE_ROOT` overrides `~/.vphone` entirely.
- Protocol (verified in upstream `sources/vphone-cli/VPhoneHostControl.swift`,
  identical at tag 1.0.12): one JSON line in, one JSON line out, connection closes.
  Commands:
  - `{"t":"screenshot"[,"path":p]}` — full-res save; ALWAYS returns compact image
  - `{"t":"tap","x":f,"y":f}`
  - `{"t":"swipe","x1":f,"y1":f,"x2":f,"y2":f[,"ms":i]}`
  - `{"t":"key","name":"home"|"power"|"volup"|"voldown"}`
  - `{"t":"type","text":s}` — sets guest clipboard (NOT text input)
  Common fields on ALL commands: `"screen":bool` (default true = include compact
  grayscale JPEG, 1/3 scale ~430x932, base64 in `image` field), `"delay":int ms`
  (default 500, settle time before capture).
  Response: `{"ok":bool[,"path":s][,"error":s][,"image":b64]}`.
- CLI surface (verified via `vphone-cli help`, live runs, upstream Swift source):
  - `boot --config <plist> [--dfu] [--headless] [--kernel-debug-port N]
    [--vphoned-bin p] [-V variant] [--install-ipa p] [--no-vphoned]` — blocking VM process
  - `patch-firmware -d <vm-dir> [-V variant] [--records-out p] [-q] [--no-binpack]
    [--no-vphoned] [--force-exc-guard] [--frida]`
  - `patch-component --component txm|kernel-base|kernel-jb -i in -o out [-q]
    [--records-out p] [--target-os v] [--frida]`
  - `vm list|info <name> [--json]` / `vm new <name> [--cpu N] [--memory MB]
    [--disk-size GB] [--rom p] [--seprom p]` / `vm config <name> [--cpu N]
    [--memory MB] [-n nat|bridged|none] [--bridge-interface if]` /
    `vm rename <old> <new>` / `vm delete <name> [--force]` /
    `vm clone <src> <new>` / `vm export <name> --out p [--max] [--include-ipsw]` /
    `vm import <archive> [--name n]` /
    `vm launch <name> [--dfu] [--headless] [-V variant] [--no-vphoned]
    [--kernel-debug-port N] [-p root] [-v]` — BLOCKING: spawns the boot binary as
    child and waits, streams guest serial console to stdio.
    `vm stop <name> [--timeout s]` — finds PIDs via `lsof -t -- <disk image>`,
    SIGINT then SIGKILL. `vm create <name> [-V variant]` — end-to-end pipeline
    (prepare→patch→restore→CFW→boot), long-running, needs sudo for CFW stage.
  - `fw catalog [--json]` / `fw prepare <name> [--iphone-source u] [--cloudos-source u]
    [--iphone-version v] [--iphone-build b] [--list] [-p root] [-v]` /
    `fw patch <name> [-V variant] [--force-exc-guard] [--frida] [-q]`
  - `restore <name> [--get-shsh] [--offline] [--udid u] [--ecid e] [-p root] [-v]`
  - `cfw install <name> [-V regular|dev|jb|exp] [--spoof-build b]
    [--force-dsc-maxslide] [--root-popup] [--keep-artifacts] [-p root] [-v]`
    — re-execs sudo by default; --root-popup uses osascript auth dialog.
  - `setup [--force] [-p root]`
  - Every vm/fw/restore/cfw subcommand accepts `-l/--library-root` (VM library root).
  - `--json` variants of `vm list`, `vm info`, `fw catalog` print machine-readable JSON.
- Variants: less|regular|dev|jb|exp (values verified).
- vphone-cli runs the Python env at ~/.vphone/venv automatically on first use;
  `setup` provisions it explicitly.
- Host: SIP disabled (verified `csrutil status`). No VM bundles exist yet
  (`~/.vphone/VMs` empty, verified).
- Existing MCP project state: vphone_mcp/{__init__,actions,client,server}.py,
  deps `mcp[cli]>=1.2.0`, entry point `vphone-mcp = vphone_mcp.server:main`,
  uv-managed. mcp.json entry already added (no VPHONE_SOCK env — discovery must work).

## Architecture

```
vphone_mcp/
  __init__.py        (unchanged)
  actions.py         (unchanged — coordinate tables stay)
  client.py          + type command, screen/delay params, image passthrough
  cli.py             NEW: VPhoneCLI runner + data-driven command registry
  server.py          + new tools (socket completion + CLI wrappers), fixed discovery
```

### client.py changes

- `VPhoneClient._send(msg, timeout=30.0)` — keep one-request-per-connection.
- Add `type_text(text, screen=True, delay=500)` → `{"t":"type","text":...}`.
- Add `screen: bool = True` and `delay: int = 500` params to tap/swipe/key/type.
- Add module helper `image_block(resp)` → returns
  `[{"type":"image","data":resp["image"],"mimeType":"image/jpeg"}]` when present, else `[]`.
- Keep returning the full resp dict from every command (server decides what to show).

### cli.py (new module)

Data-driven registry — every command is a dict entry, NOT an if-chain:

```python
CLI_BIN = env VPHONE_CLI_BIN > shutil.which("vphone-cli") >
           /Applications/vphone-cli.app/Contents/MacOS/vphone-cli (fail loudly if none)

COMMANDS = {
  "vm_list": {
    "argv": ["vm", "list"],
    "help": "...",
    "options": {
      "json": {"flag": "--json"},
      "library_root": {"option": "--library-root"},
    },
    "timeout_s": 30,
  },
  ...
}
```

Option kinds: `flag` (bare bool), `option` (--k v), `option_multi` (repeatable,
e.g. -v). Boolean params map to presence. Runner:

- `run(cmd_key, **params)` — build argv from registry entry, subprocess.run with
  timeout, capture stdout+stderr, return `{exit_code, stdout, stderr, argv}`.
  On timeout → raise with the partial output. On nonzero exit → still return output
  (tool returns it; the LLM sees the error).
- `run_background(cmd_key, **params)` — for `vm launch` only: Popen,
  start_new_session=True, stdout/stderr → log file under
  `~/.vphone/vphone-mcp/logs/<name>_launch_<ts>.log`, return `{pid, log_path}`.
- Destructive/privileged commands require an explicit `confirm=True` MCP param
  (`vm_delete` unless --force, `vm_stop`, `cfw_install`): the server tool raises
  before spawning if confirm is falsy.
- `cfw_install` passes `--root-popup` by default (MCP has no TTY for sudo);
  tool param `use_sudo=False` switches to the default sudo re-exec.

### server.py changes

- Fix `_socket_path()`: `VPHONE_SOCK` env > first existing `vphone.sock` under
  `$VPHONE_LIBRARY_ROOT` (default `~/.vphone/VMs`), glob `*/vphone.sock` >
  `$VPHONE_ROOT/VMs/*/vphone.sock` > legacy candidates. Keep failure loud.
- Add `vphone_status()` tool: reports resolved CLI binary, socket path,
  socket existence, `vm list --json` result (parsed), running launch PIDs.
- Socket tools (complete Layer 1):
  - `set_clipboard(text)` → client.type_text — docstring: sets guest clipboard.
  - Add `screen: bool = True`, `delay: int = 500` params to `tap`, `swipe`,
    the four key tools, and `set_clipboard`.
  - Every socket tool response = text status + inline `image` content block
    when the socket returned one (restores README's claim). Keep it small:
    compact grayscale JPEG from the socket, no re-encode.
  - `screenshot(path=None)` keeps full PNG behavior; also exposes its inline image.
- CLI tools (one per registry entry; tool name = registry key):
  vm_list, vm_info, vm_new, vm_config, vm_rename, vm_delete(confirm),
  vm_clone, vm_export, vm_import, vm_launch(background), vm_stop(confirm),
  vm_create, fw_catalog, fw_prepare, fw_patch, restore,
  cfw_install(confirm), patch_firmware, patch_component, setup_env.
  Each: docstring from registry help text; params generated from registry
  `options`; returns formatted stdout/stderr/exit_code.
- Keep existing 18 tools untouched except the screen/delay additions.

## Testing strategy

1. `uv sync` clean.
2. Import smoke: `uv run python -c "from vphone_mcp.server import mcp; ..."`.
3. Start server with `timeout 3 uv run vphone-mcp` → exits cleanly (stdio server
   just waits), no import errors.
4. CLI runner live: `vm list --json` (works with zero VMs — prints JSON `[]`);
   `vm info nosuchvm` returns stderr with exit code, tool returns it as text.
5. No live-VM tests possible (no bundles exist) — every socket tool must return
   a clean error message string, never a traceback.

## Out of scope (documented in README)

- Guest control channel ops (files/apps/keychain/settings/accessibility tree/
  clipboard-get) — not on the hostctl socket; needs upstream extension.
- Creating the first VM bundle (multi-GB download, sudo prompts) — available
  as MCP tools (vm_create / fw_* / restore / cfw_install) but run by the user.

## Agent work breakdown

Single implementation agent (files are tightly coupled through server.py):
1. Edit client.py (type_text, screen/delay, image_block helper).
2. Write cli.py (registry + runner).
3. Edit server.py (discovery fix, vphone_status, socket completions, CLI tool
   registration loop over registry).
4. Update README.md + CLAUDE.md tool tables.
5. Run the test strategy above and report results.

## Review gates (after agent returns)

- [ ] No if/elif chains over commands — registry lookup only.
- [ ] Every fallback fails loudly (missing binary, missing socket, unknown command).
- [ ] Existing 18 tools' signatures unchanged except screen/delay additions.
- [ ] `uv run vphone-mcp` starts; tool count matches registry + socket tools.
- [ ] CLI runner verified live against installed binary (`vm list --json`).
- [ ] JSON validity of mcp.json preserved (parse check).

## Post-implementation revisions (2026-08-31, learned from live testing)

1. **Cartograph caps upstream calls at ~60s** → long-running tools got a
   `background=True` param (registry `allow_background: True` on 10 entries;
   `vm_launch`/`boot` remain background-only). Detached children run in their
   own session and survive MCP server death; logs under
   `~/.vphone/vphone-mcp/logs/`.
2. **CFW install under `--root-popup` fails**: osascript `do shell script` runs
   in a stripped env and swallows stdout — the failure is invisible except
   cleanup noise, and it leaves cryptex mounts behind (`sudo hdiutil detach`,
   one device per invocation, then `rm -rf` `.cfw_temp`/`cfw_input`). Fix:
   `use_sudo=True` + `sudo_env` registry flag → the runner writes a 0700
   askpass helper from `VPHONE_SUDO_PASSWORD` and the CLI's own
   `sudo ${SUDO_ASKPASS:+-A}` elevates non-interactively with streaming logs.
3. **`vm_delete` confirm→CLI-force translation**: the CLI's own y/N prompt has
   no TTY in MCP and hangs (30s timeout). Generated tool now sets
   `force=True` when `confirm=True` — the MCP gate IS the human approval.
4. **`vm_export`/`vm_import`**: timeout raised 30s → 3600s + background-capable
   (GB-scale archives). Export a COLD bundle only — a running guest writes
   its disk during archive.
5. **Guest IP discovery**: serial console stays quiet about networking; the
   reliable signal is `arp -an` on the NAT bridge (bridge100). `vphone_status`
   probes launch logs first, then bridge arp (host-side `.1` filtered).
6. **Screenshot efficiency through cartograph**: image content blocks arrive
   stringified; full PNGs inline cost MBs of context. `screenshot` now takes
   `save_path` (parent dirs auto-created) and `include_full_png=False` default;
   agents read the saved PNG natively.

## Tested matrix (2026-08-31, live on jb261)

- Socket: go_home, open_app, tap, swipe, keys (home/power/vol+/vol-),
  set_clipboard, open_search, tap_back*, notification/control center, app
  switcher, page swipes, scroll up/down, screenshot (incl. save_path) — all
  verified with visual confirmation (*tap_back shares tap()'s code path).
- CLI read-only: vm_list, vm_info, fw_catalog, setup_env, vphone_status — live.
- Bundle lifecycle: vm_new, vm_config, vm_rename, vm_clone (fresh identity),
  vm_delete (confirm gate both ways) — live.
- Pipeline: fw_prepare, fw_patch (152 patches), vm_launch --dfu, restore
  (--get-shsh + restore), vm_stop, cfw_install (askpass path), vm_launch —
  built jb261 end-to-end.
- Not live-tested: patch_firmware/patch_component (share the registry runner
  and the fw_patch code path), boot (planned), vm_create end-to-end (killed by
  the 60s cap pre-fix; stages validated individually), vm_export/vm_import
  (in progress).
