#!/bin/bash
# One-command installer for the vphone-mcp LaunchAgent: serves the MCP server
# over streamable HTTP on 0.0.0.0 at login, with one fixed bearer token that
# survives restarts and re-installs (see README.md).
#
# Overridable by environment:
#   VPHONE_MCP_PORT        bind port                    (default 8765)
#   VPHONE_MCP_HOST        bind address                 (default 0.0.0.0)
#   VPHONE_MCP_PUBLIC_URL  URL clients dial             (default http://<LAN IP>:<port>)
#   VPHONE_MCP_AUTH_TOKEN  use this token instead of generating/reusing one
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

HOST="${VPHONE_MCP_HOST:-0.0.0.0}"
PORT="${VPHONE_MCP_PORT:-8765}"
VPHONE_ROOT="${VPHONE_ROOT:-$HOME_DIR/.vphone}"
# Deliberately NOT under $VPHONE_ROOT/vphone-mcp: that directory is what the
# /files endpoint serves.
TOKEN_FILE="$VPHONE_ROOT/mcp-auth-token"
AGENT_PLIST="$HOME_DIR/Library/LaunchAgents/$LABEL.plist"
LOG_PATH="$HOME_DIR/Library/Logs/vphone-mcp.log"
EXEC="$REPO_DIR/.venv/bin/vphone-mcp"

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
# once on the first run.
mkdir -p "$VPHONE_ROOT"
if [[ -n "${VPHONE_MCP_AUTH_TOKEN:-}" ]]; then
    printf '%s\n' "$VPHONE_MCP_AUTH_TOKEN" > "$TOKEN_FILE"
    TOKEN_ORIGIN="from \$VPHONE_MCP_AUTH_TOKEN"
elif [[ -s "$TOKEN_FILE" ]]; then
    TOKEN_ORIGIN="reused from $TOKEN_FILE"
else
    openssl rand -hex 32 > "$TOKEN_FILE"
    TOKEN_ORIGIN="generated"
fi
chmod 600 "$TOKEN_FILE"
TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
[[ -n "$TOKEN" ]] || { echo "error: $TOKEN_FILE is empty" >&2; exit 1; }

# --- the URL clients dial ---------------------------------------------
PRIMARY_IF="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
LAN_IP="$(ipconfig getifaddr "${PRIMARY_IF:-en0}" 2>/dev/null || true)"
PUBLIC_URL="${VPHONE_MCP_PUBLIC_URL:-http://${LAN_IP:-127.0.0.1}:$PORT}"
PUBLIC_URL="${PUBLIC_URL%/}"

# --- cleanup any previous install -------------------------------------
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
    HEALTH="$(curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || true)"
    [[ -n "$HEALTH" ]] && break
    sleep 1
done
STATE="$(launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | awk '/^\tstate = /{print $3; exit}')"
UNAUTH="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
    -X POST "http://127.0.0.1:$PORT/mcp" 2>/dev/null || true)"
REMOTE=""
if [[ -n "$LAN_IP" ]]; then
    REMOTE="$(curl -fsS --max-time 2 "http://$LAN_IP:$PORT/healthz" 2>/dev/null || true)"
fi

# Accessibility is granted per asking process, so only the agent itself can
# answer for the agent: ask it over MCP rather than probing from this shell.
mcp_tool() {
    local base="http://127.0.0.1:$PORT/mcp" sid
    local hdr=(-H "Authorization: Bearer $TOKEN"
               -H "Content-Type: application/json"
               -H "Accept: application/json, text/event-stream")
    sid="$(curl -s -D- -o /dev/null --max-time 5 -X POST "$base" "${hdr[@]}" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"install.sh","version":"1"}}}' \
        2>/dev/null | tr -d '\r' | awk 'tolower($1)=="mcp-session-id:"{print $2}')"
    [[ -n "$sid" ]] || return 1
    curl -s -o /dev/null --max-time 5 -X POST "$base" "${hdr[@]}" -H "mcp-session-id: $sid" \
        -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' 2>/dev/null
    curl -s --max-time 20 -X POST "$base" "${hdr[@]}" -H "mcp-session-id: $sid" \
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
[[ -n "$LAN_IP" ]] && echo "via $LAN_IP:      ${REMOTE:-(no response — check the macOS firewall)}"
echo "log tail:"
tail -n 5 "$LOG_PATH" 2>/dev/null | sed 's/^/  /' || echo "  (no log yet)"
echo
if [[ "$HEALTH" != *'"ok"'* ]]; then
    echo "NOTE: the server did not answer — see $LOG_PATH and README.md."
    exit 1
fi

cat <<EOF
OK. vphone-mcp is serving $HOST:$PORT and starts at login.

  token ($TOKEN_ORIGIN, 0600 at $TOKEN_FILE):
    $TOKEN

  add it to a client:
    claude mcp add --transport http vphone $PUBLIC_URL/mcp \\
      --header "Authorization: Bearer \$(cat $TOKEN_FILE)"

Plain HTTP on a LAN address: the token travels in cleartext. Put a TLS
proxy or a tunnel (Tailscale, Cloudflare Tunnel) in front of it before
this leaves a trusted network.
EOF

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
