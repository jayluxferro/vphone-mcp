#!/bin/bash
# One-command installer for the vphone-mcp LaunchAgent: serves the MCP server
# over streamable HTTP on 127.0.0.1 at login, with one fixed bearer token that
# survives restarts and re-installs (see README.md). Remote clients reach the
# loopback port through an SSH tunnel or Tailscale serve, so neither the token
# nor the VM host is exposed on the LAN in cleartext.
#
# Overridable by environment:
#   VPHONE_MCP_PORT           bind port                 (default 8765)
#   VPHONE_MCP_HOST           bind address              (default 127.0.0.1;
#                             0.0.0.0 serves plain HTTP to the whole LAN)
#   VPHONE_MCP_PUBLIC_URL     URL clients dial          (default http://<host>:<port>,
#                             the primary LAN IP for a 0.0.0.0 bind)
#   VPHONE_MCP_ALLOWED_HOSTS  extra Host headers to accept, comma-separated
#                             (e.g. a Tailscale serve name; see README.md)
#   VPHONE_MCP_AUTH_TOKEN     use this token instead of generating/reusing one
#
# Flags:
#   --open-accessibility   also open the Accessibility pane and reveal the
#                          binary to drop into it (native screen recording)
set -euo pipefail

OPEN_ACCESSIBILITY=0
for arg in "$@"; do
    case "$arg" in
        --open-accessibility) OPEN_ACCESSIBILITY=1 ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

LABEL="com.vphone.mcp"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$HERE/../.." && pwd)"
HOME_DIR="$HOME"
UID_NUM="$(id -u)"

HOST="${VPHONE_MCP_HOST:-127.0.0.1}"
PORT="${VPHONE_MCP_PORT:-8765}"
VPHONE_ROOT="${VPHONE_ROOT:-$HOME_DIR/.vphone}"
# Deliberately NOT under $VPHONE_ROOT/vphone-mcp: that directory is what the
# /files endpoint serves.
TOKEN_FILE="$VPHONE_ROOT/mcp-auth-token"
AGENT_PLIST="$HOME_DIR/Library/LaunchAgents/$LABEL.plist"
LOG_PATH="$HOME_DIR/Library/Logs/vphone-mcp.log"
EXEC="$REPO_DIR/.venv/bin/vphone-mcp"

is_loopback() { [[ "$1" == 127.* || "$1" == "localhost" || "$1" == "::1" ]]; }
is_wildcard() { [[ "$1" == "0.0.0.0" || "$1" == "::" ]]; }
# host:port as a URL authority (IPv6 literals need brackets).
authority() { if [[ "$1" == *:* ]]; then echo "[$1]:$2"; else echo "$1:$2"; fi; }

# --- dependencies -----------------------------------------------------
if [[ ! -x "$EXEC" ]]; then
    echo "==> Creating the venv ($REPO_DIR)…"
    (cd "$REPO_DIR" && uv sync) || {
        echo "error: uv sync failed — install uv or run it by hand" >&2
        exit 1
    }
fi
[[ -x "$EXEC" ]] || { echo "error: $EXEC missing after uv sync" >&2; exit 1; }

CLI_BIN="${VPHONE_CLI_BIN:-}"
if [[ -z "$CLI_BIN" ]]; then
    CLI_BIN="$(command -v vphone-cli || true)"
fi
if [[ -z "$CLI_BIN" && -x /Applications/vphone-cli.app/Contents/MacOS/vphone-cli ]]; then
    CLI_BIN="/Applications/vphone-cli.app/Contents/MacOS/vphone-cli"
fi
[[ -n "$CLI_BIN" ]] || {
    echo "error: vphone-cli not found — install it or set VPHONE_CLI_BIN" >&2
    exit 1
}

# The binary macOS attributes permissions to is the interpreter behind the
# console script, not the script and not the terminal this installer runs in.
AGENT_BIN="$("$REPO_DIR/.venv/bin/python" -c 'import os,sys; print(os.path.realpath(sys.executable))')"

# --- the fixed token --------------------------------------------------
# Reused across re-installs so configured clients keep working; generated
# once on the first run. The value is never printed: installer output ends up
# in scrollback, screenshots and agent transcripts, and the file is where
# clients should read it from.
mkdir -p "$VPHONE_ROOT"
write_token() {
    # Created under umask 077 and renamed into place, so the token is never
    # readable by anyone else, not even for the moment between creating a
    # file and chmod-ing it. The rename also replaces an old token atomically.
    (
        umask 077
        tmp="$(mktemp "$TOKEN_FILE.XXXXXX")"
        printf '%s\n' "$1" > "$tmp"
        mv -f "$tmp" "$TOKEN_FILE"
    )
}
if [[ -n "${VPHONE_MCP_AUTH_TOKEN:-}" ]]; then
    write_token "$VPHONE_MCP_AUTH_TOKEN"
    TOKEN_ORIGIN="from \$VPHONE_MCP_AUTH_TOKEN"
elif [[ -s "$TOKEN_FILE" ]]; then
    TOKEN_ORIGIN="reused"
else
    NEW_TOKEN="$(openssl rand -hex 32)"
    write_token "$NEW_TOKEN"
    unset NEW_TOKEN
    TOKEN_ORIGIN="generated"
fi
chmod 600 "$TOKEN_FILE"  # a file left by an older installer may be looser
TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
[[ -n "$TOKEN" ]] || { echo "error: $TOKEN_FILE is empty" >&2; exit 1; }

# --- the URL clients dial ---------------------------------------------
LAN_IP=""
if is_wildcard "$HOST"; then
    PRIMARY_IF="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}' || true)"
    LAN_IP="$(ipconfig getifaddr "${PRIMARY_IF:-en0}" 2>/dev/null || true)"
    DEFAULT_URL="http://${LAN_IP:-127.0.0.1}:$PORT"
    CHECK_BASE="http://127.0.0.1:$PORT"
else
    DEFAULT_URL="http://$(authority "$HOST" "$PORT")"
    CHECK_BASE="$DEFAULT_URL"
fi
PUBLIC_URL="${VPHONE_MCP_PUBLIC_URL:-$DEFAULT_URL}"
PUBLIC_URL="${PUBLIC_URL%/}"

# --- Host headers to accept -------------------------------------------
# An explicit list replaces the server's defaults rather than extending them,
# so keep the loopback names in it: the checks below and an SSH tunnel both
# dial 127.0.0.1. Empty means the server's default for the bind address.
ALLOWED_HOSTS="${VPHONE_MCP_ALLOWED_HOSTS:-}"
if [[ -n "$ALLOWED_HOSTS" ]]; then
    ALLOWED_HOSTS="127.0.0.1:*,localhost:*,[::1]:*,$ALLOWED_HOSTS"
fi

# --- cleanup any previous install -------------------------------------
# Remember what the old agent bound, so a re-install that narrows it (the
# default moved from 0.0.0.0 to 127.0.0.1) says so instead of silently
# cutting off LAN clients.
PREV_HOST=""
if [[ -f "$AGENT_PLIST" ]]; then
    PREV_HOST="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments' "$AGENT_PLIST" 2>/dev/null \
        | awk 'take {print $1; exit} $1 == "--host" {take = 1}' || true)"
fi
echo "==> Cleaning up previous installs (ignore not-found errors)…"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
rm -f "$AGENT_PLIST"

# --- generate ---------------------------------------------------------
mkdir -p "$HOME_DIR/Library/LaunchAgents" "$(dirname "$LOG_PATH")"
GENERATED="$VPHONE_ROOT/vphone-mcp/$LABEL.plist"
mkdir -p "$(dirname "$GENERATED")"

sed \
    -e "s|{{LABEL}}|$LABEL|g" \
    -e "s|{{EXEC}}|$EXEC|g" \
    -e "s|{{HOST}}|$HOST|g" \
    -e "s|{{PORT}}|$PORT|g" \
    -e "s|{{TOKEN_FILE}}|$TOKEN_FILE|g" \
    -e "s|{{PUBLIC_URL}}|$PUBLIC_URL|g" \
    -e "s|{{ALLOWED_HOSTS}}|$ALLOWED_HOSTS|g" \
    -e "s|{{CLI_BIN}}|$CLI_BIN|g" \
    -e "s|{{PATH_VALUE}}|$(dirname "$CLI_BIN"):/usr/bin:/bin:/usr/sbin:/sbin|g" \
    -e "s|{{HOME_DIR}}|$HOME_DIR|g" \
    -e "s|{{WORKDIR}}|$REPO_DIR|g" \
    -e "s|{{LOG_PATH}}|$LOG_PATH|g" \
    "$HERE/com.vphone.mcp.plist.template" \
    > "$GENERATED"

plutil -lint "$GENERATED" >/dev/null \
    || { echo "error: generated plist failed validation" >&2; exit 1; }

# --- install ----------------------------------------------------------
echo "==> Installing LaunchAgent…"
cp "$GENERATED" "$AGENT_PLIST"
chmod 644 "$AGENT_PLIST"
launchctl bootstrap "gui/$UID_NUM" "$AGENT_PLIST"
launchctl kickstart "gui/$UID_NUM/$LABEL" 2>/dev/null || true

# --- verify -----------------------------------------------------------
echo "==> Verifying…"
HEALTH=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    HEALTH="$(curl -fsS --max-time 2 "$CHECK_BASE/healthz" 2>/dev/null || true)"
    [[ -n "$HEALTH" ]] && break
    sleep 1
done
STATE="$(launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | awk '/^\tstate = /{print $3; exit}' || true)"
UNAUTH="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
    -X POST "$CHECK_BASE/mcp" 2>/dev/null || true)"
REMOTE=""
if [[ -n "$LAN_IP" ]]; then
    REMOTE="$(curl -fsS --max-time 2 "http://$LAN_IP:$PORT/healthz" 2>/dev/null || true)"
fi

# Accessibility is granted per asking process, so only the agent itself can
# answer for the agent: ask it over MCP rather than probing from this shell.
# The bearer header reaches curl through a file descriptor (-H @file), never
# argv, because any local user can read argv with ps.
auth_header() { printf 'Authorization: Bearer %s\n' "$TOKEN"; }
mcp_tool() {
    local base="$CHECK_BASE/mcp" sid
    local hdr=(-H "Content-Type: application/json"
               -H "Accept: application/json, text/event-stream")
    sid="$(curl -s -D- -o /dev/null --max-time 5 -X POST "$base" \
        -H @<(auth_header) "${hdr[@]}" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"install.sh","version":"1"}}}' \
        2>/dev/null | tr -d '\r' | awk 'tolower($1)=="mcp-session-id:"{print $2}')"
    [[ -n "$sid" ]] || return 1
    curl -s -o /dev/null --max-time 5 -X POST "$base" \
        -H @<(auth_header) "${hdr[@]}" -H "mcp-session-id: $sid" \
        -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' 2>/dev/null
    curl -s --max-time 20 -X POST "$base" \
        -H @<(auth_header) "${hdr[@]}" -H "mcp-session-id: $sid" \
        -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":{}}}" \
        2>/dev/null | sed -n 's/^data: //p' | "$REPO_DIR/.venv/bin/python" -c \
        'import sys,json;d=json.load(sys.stdin);print(d.get("result",{}).get("content",[{}])[0].get("text",""))'
}
ACCESS_LINE="$(mcp_tool vphone_status 2>/dev/null | grep -i '^accessibility' || true)"

echo
echo "agent state:      ${STATE:-unknown}"
echo "GET /healthz:     ${HEALTH:-(no response)}"
echo "POST /mcp no auth: ${UNAUTH:-?} (401 = the token is enforced)"
echo "${ACCESS_LINE:-accessibility: (could not ask the agent)}"
if [[ -n "$LAN_IP" ]]; then
    echo "via $LAN_IP:      ${REMOTE:-(no response — check the macOS firewall)}"
elif is_loopback "$HOST"; then
    echo "exposure:         loopback only (remote clients: SSH tunnel or Tailscale serve)"
fi
echo "log tail:"
tail -n 5 "$LOG_PATH" 2>/dev/null | sed 's/^/  /' || echo "  (no log yet)"
echo
if [[ "$HEALTH" != *'"ok"'* ]]; then
    echo "NOTE: the server did not answer — see $LOG_PATH and README.md."
    exit 1
fi

cat <<EOF
OK. vphone-mcp is serving $HOST:$PORT and starts at login.

  token: $TOKEN_FILE ($TOKEN_ORIGIN, mode 0600)
    not printed here; clients read it from that file

  add it to a client on this Mac:
    claude mcp add --transport http vphone $PUBLIC_URL/mcp \\
      --header "Authorization: Bearer \$(cat $TOKEN_FILE)"
EOF

if is_loopback "$HOST"; then
    SSH_TARGET="$(id -un)@$(scutil --get LocalHostName 2>/dev/null || hostname -s).local"
    cat <<EOF

  from another machine, tunnel the port over SSH (needs Remote Login on this
  Mac; keep the local port $PORT so file links work through the same tunnel):
    ssh -N -L $PORT:127.0.0.1:$PORT $SSH_TARGET &
    claude mcp add --transport http vphone http://127.0.0.1:$PORT/mcp \\
      --header "Authorization: Bearer \$(ssh $SSH_TARGET cat $TOKEN_FILE)"

  or put Tailscale serve in front of it (HTTPS, tailnet only); see README.md.
EOF
else
    cat <<EOF

Plain HTTP on $HOST: the bearer token and file links travel in cleartext.
Prefer the default 127.0.0.1 bind with an SSH tunnel or Tailscale serve, or
put a TLS proxy in front of it before this leaves a trusted network.
EOF
fi

if [[ -n "$PREV_HOST" && "$PREV_HOST" != "$HOST" ]]; then
    cat <<EOF

NOTE: the previous install bound $PREV_HOST; this one binds $HOST. Clients
that dialled the old address need the route above, or re-run with
VPHONE_MCP_HOST=$PREV_HOST to keep the old exposure.
EOF
fi

# --- accessibility (native screen recording) --------------------------
# macOS has no supported way to grant this from a script: the entry lives in
# the SIP-protected system TCC database, and tccutil only resets. So the
# installer does the part it can — naming the exact binary (the interpreter,
# which nobody would guess) and opening the pane.
if [[ "$ACCESS_LINE" != *granted* ]]; then
    cat <<EOF

Native screen recording (vphone-cli's own encoder) also needs Accessibility,
which only you can grant — macOS has no CLI for it. Add this binary:

  $AGENT_BIN

  open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
  open -R "$AGENT_BIN"          # reveal it in Finder to drag it in

then: launchctl kickstart -k gui/$UID_NUM/$LABEL && $HERE/install.sh

The agent's interpreter is what counts, not your terminal: macOS attributes
the permission to the process that asks. If it still says DENIED after the
restart, add /usr/bin/osascript too. The grant follows that exact path, so
re-run this installer after a Python upgrade moves it.

On a managed fleet, an MDM PPPC profile grants it without the clicking. Until
then, recording still works — start_recording(mode="poll") captures over the
socket and needs no permission.
EOF
    if (( OPEN_ACCESSIBILITY )); then
        open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility' 2>/dev/null || true
        open -R "$AGENT_BIN" 2>/dev/null || true
    fi
fi
