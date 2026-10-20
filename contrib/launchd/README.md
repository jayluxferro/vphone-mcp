# vphone-mcp as a LaunchAgent (HTTP on `127.0.0.1`)

Runs the MCP server over streamable HTTP for remote clients, starting at login
and restarting if it dies, with one **fixed** bearer token that survives
restarts and re-installs. It listens on loopback only; other machines reach it
through an SSH tunnel or Tailscale serve (below), so the token never crosses a
network in cleartext.

```bash
./contrib/launchd/install.sh
```

The installer prints where the token is (never the token itself) and
ready-to-paste `claude mcp add` lines, for this Mac and for another machine
over SSH.

## What it installs

| | |
|---|---|
| LaunchAgent | `~/Library/LaunchAgents/com.vphone.mcp.plist` (gui domain, your uid) |
| Command | `<repo>/.venv/bin/vphone-mcp --transport streamable-http --host 127.0.0.1 --port 8765` |
| Token | `~/.vphone/mcp-auth-token`, mode 0600, `openssl rand -hex 32` |
| Log | `~/Library/Logs/vphone-mcp.log` |
| Endpoints | `/mcp` (bearer), `/files/<path>` (signed per-file links), `/healthz` (open) |

A **user agent, not a system daemon**: the tools talk to the hostctl socket
under `~/.vphone/machines` and drive a VM owned by your login session, so the server
has to be you. It starts when you log in, not at boot.

## Reaching it from another machine

The agent binds `127.0.0.1`, so nothing on the LAN can connect to it directly.
Pick one way in:

**SSH tunnel** (no setup beyond Remote Login in System Settings → General →
Sharing). On the client:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@vm-host.local &
claude mcp add --transport http vphone http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $(ssh you@vm-host.local cat ~/.vphone/mcp-auth-token)"
```

Keep the client-side port the same as the server's (8765). File links are
built on `http://127.0.0.1:8765`, which then means "this end of the tunnel" on
the client too, so `curl -O '<link>'` works there unchanged.

**Tailscale serve** (HTTPS with a `ts.net` certificate, reachable only from your
tailnet). On the VM host:

```bash
tailscale serve --bg 8765        # proxies https://<mac>.<tailnet>.ts.net → 127.0.0.1:8765
VPHONE_MCP_PUBLIC_URL=https://<mac>.<tailnet>.ts.net \
VPHONE_MCP_ALLOWED_HOSTS=<mac>.<tailnet>.ts.net \
  ./contrib/launchd/install.sh
```

Then point clients at `https://<mac>.<tailnet>.ts.net/mcp` with the same bearer
header. `VPHONE_MCP_PUBLIC_URL` makes file links use that name.
`VPHONE_MCP_ALLOWED_HOSTS` is there because a loopback bind keeps the SDK's
DNS-rebinding Host check on, so a proxy that forwards the original Host header
would otherwise get HTTP 421. The installer keeps the loopback names in that
list, so local access keeps working. The `serve` syntax above is for current
Tailscale releases; older ones differ, see `tailscale serve --help`. Check it
from another tailnet device with `curl https://<mac>.<tailnet>.ts.net/healthz`.

**LAN bind (opt-in).** `VPHONE_MCP_HOST=0.0.0.0 ./contrib/launchd/install.sh`
restores the old behaviour: plain HTTP on every interface, with file links on
the primary LAN IP. The bearer token and every link then travel in
cleartext, so use it only on a network you trust, or put TLS in front of it.

Re-running the installer over an agent that bound something else (an install
from before loopback became the default, say) prints a NOTE saying so, so LAN
clients don't silently stop working.

## The fixed token

The token lives in a file, and the plist points `VPHONE_MCP_AUTH_TOKEN_FILE` at
it — the value itself is never in argv (visible in `ps`) and never in the plist
(`launchctl print` shows a job's environment). The file sits at
`~/.vphone/mcp-auth-token`, deliberately *outside* `~/.vphone/vphone-mcp`,
which is the directory the `/files` endpoint serves.

The installer handles the value carefully:

- It never prints the token, because installer output ends up in scrollback,
  screenshots and agent transcripts. Clients read the file instead (`cat` it
  locally, `ssh … cat` it remotely), as in the lines above.
- It writes the file under `umask 077` through `mktemp` plus a rename, so the
  token is never world-readable, not even for the moment between creating
  the file and chmod-ing it. A looser file left by an older installer is
  tightened to 0600.
- Its own verification calls hand the bearer header to curl through a file
  descriptor (`-H @<(…)`), never argv.

The running server also drops `VPHONE_MCP_AUTH_TOKEN` from its environment once
it has read it. Child processes (vphone-cli, ffmpeg, osascript) therefore never
inherit the token, and their output, which comes back in tool results and
served launch logs, cannot echo it.

`install.sh` reuses an existing token, so re-running it after a code change
leaves every configured client working. To pin a value of your own:

```bash
VPHONE_MCP_AUTH_TOKEN="my-fixed-token" ./contrib/launchd/install.sh
```

To rotate: `./contrib/launchd/uninstall.sh --purge-token && ./contrib/launchd/install.sh`.
Rotating also revokes every file link handed out so far, because links are
signed with a key derived from the token.

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
| `VPHONE_MCP_HOST` | `127.0.0.1` (`0.0.0.0` = plain HTTP on the LAN) |
| `VPHONE_MCP_PORT` | `8765` |
| `VPHONE_MCP_PUBLIC_URL` | `http://<host>:<port>`; the primary LAN IP for a `0.0.0.0` bind |
| `VPHONE_MCP_ALLOWED_HOSTS` | empty (the server's default for the bind); loopback names are kept when set |
| `VPHONE_MCP_AUTH_TOKEN` | reuse the token file, else generate |
| `VPHONE_CLI_BIN` | `vphone-cli` on PATH, else the app bundle |

`VPHONE_MCP_PUBLIC_URL` is what file links (`screenshot()`, `get_file_url()`,
the launch logs) are built on, so it must be the URL clients dial. On the
default loopback bind that is `http://127.0.0.1:<port>`, which is right for
this Mac and for an SSH tunnel that keeps the port. Set it explicitly behind
Tailscale serve, a proxy, or a stable hostname.

The plist also pins `VPHONE_CLI_BIN` and a `PATH`, because a launchd agent
inherits neither your shell's PATH nor your profile. Other server settings
(e.g. `VPHONE_MCP_LINK_TTL_SECONDS`) can go in the template's
`EnvironmentVariables`; the installer regenerates the plist on every run, so
edit the template, not the installed copy.

## Security

- **Loopback by default.** Nothing off this Mac can connect. Remote access goes
  through SSH or Tailscale, both of which encrypt the token in transit. With
  `VPHONE_MCP_HOST=0.0.0.0` the token crosses the network in cleartext, so
  only use it on a trusted LAN, or front it with TLS (a reverse proxy, or
  `--ssl-certfile`/`--ssl-keyfile` added to the template's `ProgramArguments`).
- **The token is the only gate**, and the tools behind it launch VMs and run
  vphone-cli under sudo. Anyone who can reach the port and holds the token has
  the VM host. That includes host paths outside the served directory, through
  path parameters such as `screenshot(save_path=…)` and `vm_export(out=…)`; see
  the main README.
- **File links are not the token.** Each one is an HMAC-signed capability for
  one file and one method (GET, or PUT for an upload), expiring after an hour
  by default (`VPHONE_MCP_LINK_TTL_SECONDS`). A link pasted into a chat or a
  transcript exposes that one file until it expires, not the server.
- On the loopback bind the SDK's DNS-rebinding Host allowlist stays on.
  Binding a non-loopback address turns it off by design (see
  `remote._transport_security`); `VPHONE_MCP_ALLOWED_HOSTS` turns it back on for
  known names.
- The first bind to `0.0.0.0` can raise the macOS firewall's "accept incoming
  connections" prompt for the venv's `python3`. If the installer's LAN check
  comes back empty, that prompt (or an existing deny rule) is the usual cause.
  The loopback default never triggers it.

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
