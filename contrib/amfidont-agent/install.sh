#!/bin/bash
# One-command installer for the amfidont login-agent (see README.md for the
# why). Generates the machine-specific files, installs them, reloads the
# agent, and verifies. Asks for your password once for the sudoers step.
set -euo pipefail

APP_PATH="/Applications/vphone-cli.app"
USER_NAME="$(id -un)"
HOME_DIR="$(eval echo ~"$USER_NAME")"
AGENT_DIR="$HOME_DIR/.vphone/amfidont-agent"
LOG_PATH="$HOME_DIR/Library/Logs/vphone-amfidont.log"
LABEL="com.vphone.amfidont"
AGENT_PLIST="$HOME_DIR/Library/LaunchAgents/$LABEL.plist"
SUDOERS_DEST="/etc/sudoers.d/vphone-amfidont"

# --- resolve this machine's paths -------------------------------------
PYTHON_BIN="$(xcrun --find python3)" || {
    echo "error: xcrun python3 not found — install Xcode or CLT" >&2
    exit 1
}
SITE_PACKAGES="$("$PYTHON_BIN" -m site --user-site)"
DEVELOPER_DIR="$(xcode-select -p)"

# --- generate ---------------------------------------------------------
mkdir -p "$AGENT_DIR" "$HOME_DIR/Library/LaunchAgents"
HERE="$(cd "$(dirname "$0")" && pwd)"

sed \
    -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
    -e "s|{{APP_PATH}}|$APP_PATH|g" \
    -e "s|{{DEVELOPER_DIR}}|$DEVELOPER_DIR|g" \
    -e "s|{{SITE_PACKAGES}}|$SITE_PACKAGES|g" \
    -e "s|{{LOG_PATH}}|$LOG_PATH|g" \
    "$HERE/com.vphone.amfidont.plist.template" \
    > "$AGENT_DIR/com.vphone.amfidont.plist"

sed \
    -e "s|{{USER}}|$USER_NAME|g" \
    -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
    "$HERE/vphone-amfidont.sudoers.template" \
    > "$AGENT_DIR/vphone-amfidont.sudoers"

plutil -lint "$AGENT_DIR/com.vphone.amfidont.plist" >/dev/null \
    || { echo "error: generated plist failed validation" >&2; exit 1; }

# Validate the sudoers file BEFORE installing it — a broken sudoers can
# brick sudo. visudo -c -f validates any path without root.
visudo -c -f "$AGENT_DIR/vphone-amfidont.sudoers" \
    || { echo "error: generated sudoers failed validation" >&2; exit 1; }

# --- install ----------------------------------------------------------
echo "==> Installing sudoers (password prompt)…"
sudo cp "$AGENT_DIR/vphone-amfidont.sudoers" "$SUDOERS_DEST"
sudo chown root:wheel "$SUDOERS_DEST"
sudo chmod 0440 "$SUDOERS_DEST"
sudo visudo -c

echo "==> Installing LaunchAgent…"
cp "$AGENT_DIR/com.vphone.amfidont.plist" "$AGENT_PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST"

# --- verify -----------------------------------------------------------
sleep 3
STATE="$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -m1 state | awk '{print $3}')"
echo
echo "agent state: ${STATE:-unknown}"
echo "log tail:"
tail -n 3 "$LOG_PATH" 2>/dev/null || echo "  (no log yet)"
echo
if [[ "$STATE" == "running" ]]; then
    echo "OK. The agent is running and survives reboots."
else
    echo "NOTE: agent not running — check the log above and the README's verify section."
fi
