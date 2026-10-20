# vphone-mcp as a LaunchAgent (HTTP on `0.0.0.0`)

Runs the MCP server over streamable HTTP for remote clients, starting at login
and restarting if it dies, with one **fixed** bearer token that survives
restarts and re-installs.

```bash
./contrib/launchd/install.sh
```

The installer prints the token and a ready-to-paste `claude mcp add` line.

## What it installs

| | |
|---|---|
| LaunchAgent | `~/Library/LaunchAgents/com.vphone.mcp.plist` (gui domain, your uid) |
| Command | `<repo>/.venv/bin/vphone-mcp --transport streamable-http --host 0.0.0.0 --port 8765` |
| Token | `~/.vphone/mcp-auth-token`, mode 0600, `openssl rand -hex 32` |
| Log | `~/Library/Logs/vphone-mcp.log` |
| Endpoints | `/mcp` (bearer), `/files/<path>` (basic), `/healthz` (open) |

A **user agent, not a system daemon**: the tools talk to the hostctl socket
under `~/.vphone/VMs` and drive a VM owned by your login session, so the server
has to be you. It starts when you log in, not at boot.

## The fixed token

The token lives in a file, and the plist points `VPHONE_MCP_AUTH_TOKEN_FILE` at
it — the value itself is never in argv (visible in `ps`) and never in the plist
(`launchctl print` shows a job's environment). The file sits at
`~/.vphone/mcp-auth-token`, deliberately *outside* `~/.vphone/vphone-mcp`,
which is the directory the `/files` endpoint serves.

`install.sh` reuses an existing token, so re-running it after a code change
leaves every configured client working. To pin a value of your own:

```bash
VPHONE_MCP_AUTH_TOKEN="my-fixed-token" ./contrib/launchd/install.sh
```

To rotate: `./contrib/launchd/uninstall.sh --purge-token && ./contrib/launchd/install.sh`.

## Accessibility (native screen recording)

`start_recording(mode="native")` drives vphone-cli's own recorder through its
menu, which needs the macOS Accessibility permission. **No script can grant it**
— the entry lives in the SIP-protected system TCC database and `tccutil` only
resets — so the installer does the parts it can:

- it asks the **running agent** whether it has the permission (the answer is
  per-process: your terminal having it says nothing about the LaunchAgent), and
  prints the result in the verification block;
- it names the exact binary to add — the interpreter behind the console script,
  e.g. `~/.local/share/uv/python/cpython-3.12.*/bin/python3.12`, not
  `.venv/bin/vphone-mcp` and not your terminal;
- `./install.sh --open-accessibility` opens the Accessibility pane and reveals
  that binary in Finder so it can be dragged straight in.

After granting:

```bash
launchctl kickstart -k gui/$(id -u)/com.vphone.mcp && ./contrib/launchd/install.sh
```

The re-run re-checks and should print `accessibility (native recording):
granted`. Two things to know about that one click:

- If it still says `DENIED` after the restart, add `/usr/bin/osascript` as well.
  macOS normally attributes the request to the responsible process (the agent),
  but the script interpreter can end up being the one asked about.
- The grant is tied to the binary's **path**, which carries the Python version
  (`…/cpython-3.12.12-…/bin/python3.12`). If a later `uv sync` moves to another
  Python, the path changes and the permission silently stops applying — the
  installer's check is what catches it, so re-run it after an upgrade.

On a managed fleet, an MDM PPPC profile grants it without any clicking. Until
then nothing is blocked: `start_recording(mode="poll")` captures over the host
control socket and needs no permission, and `mode="auto"` (the default) falls
back to it automatically and says why.

## Overrides

`install.sh` reads these from the environment:

| Variable | Default |
|---|---|
| `VPHONE_MCP_HOST` | `0.0.0.0` |
| `VPHONE_MCP_PORT` | `8765` |
| `VPHONE_MCP_PUBLIC_URL` | `http://<primary LAN IP>:<port>` |
| `VPHONE_MCP_AUTH_TOKEN` | reuse the token file, else generate |
| `VPHONE_CLI_BIN` | `vphone-cli` on PATH, else the app bundle |

`VPHONE_MCP_PUBLIC_URL` matters because the bind address is `0.0.0.0`: file
links (`screenshot()`, `get_file_url()`, the launch logs) are built on it, and
would otherwise point at `127.0.0.1` — unfetchable from another machine. Set it
explicitly when the server sits behind a proxy, a tunnel, or a stable hostname
rather than a DHCP lease.

The plist also pins `VPHONE_CLI_BIN` and a `PATH`, because a launchd agent
inherits neither your shell's PATH nor your profile.

## Security

- **Plain HTTP.** The bearer token crosses the network in cleartext, so this is
  a trusted-LAN configuration. Before it is reachable from anywhere else, front
  it with TLS: a reverse proxy, Tailscale, a Cloudflare Tunnel, or
  `--ssl-certfile`/`--ssl-keyfile` added to the plist's `ProgramArguments`.
- **The token is the only gate**, and the tools behind it launch VMs and run
  vphone-cli under sudo. Anyone who can reach the port and holds the token has
  the VM host.
- Binding a non-loopback address turns off the SDK's DNS-rebinding host
  allowlist by design (see `remote._transport_security`). Add
  `--allowed-host <name>:*` to `ProgramArguments` to turn it back on for a known
  hostname.
- The first bind to `0.0.0.0` can raise the macOS firewall's "accept incoming
  connections" prompt for the venv's `python3`. If the LAN check in the
  installer comes back empty, that prompt (or an existing deny rule) is the
  usual cause.

## Operating it

```bash
tail -f ~/Library/Logs/vphone-mcp.log          # logs
launchctl print gui/$(id -u)/com.vphone.mcp    # state, pid, last exit
launchctl kickstart -k gui/$(id -u)/com.vphone.mcp  # restart
curl -s localhost:8765/healthz                 # liveness, no token needed
./contrib/launchd/uninstall.sh                 # remove
```

After pulling code or changing dependencies, `uv sync` then the `kickstart -k`
above — the plist points at the venv, so nothing needs reinstalling unless the
flags change.
