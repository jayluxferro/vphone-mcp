#!/bin/bash
# Generate the amfidont login-agent files for THIS machine and print the
# one-time install commands (two need sudo). See README.md for the why.
set -euo pipefail

APP_PATH="/Applications/vphone-cli.app"
USER_NAME="$(id -un)"
HOME_DIR="$(eval echo ~"$USER_NAME")"
AGENT_DIR="$HOME_DIR/.vphone/amfidont-agent"
LOG_PATH="$HOME_DIR/Library/Logs/vphone-amfidont.log"

# The interpreter that has the amfidont module installed (the pip install
# in vphone-amfidont uses xcrun python3).
PYTHON_BIN="$(xcrun --find python3)" || {
    echo "error: xcrun python3 not found — install Xcode or CLT" >&2
    exit 1
}
SITE_PACKAGES="$("$PYTHON_BIN" -m site --user-site)"
DEVELOPER_DIR="$(xcode-select -p)"

mkdir -p "$AGENT_DIR"

sed \
    -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
    -e "s|{{APP_PATH}}|$APP_PATH|g" \
    -e "s|{{DEVELOPER_DIR}}|$DEVELOPER_DIR|g" \
    -e "s|{{SITE_PACKAGES}}|$SITE_PACKAGES|g" \
    -e "s|{{LOG_PATH}}|$LOG_PATH|g" \
    "$(dirname "$0")/com.vphone.amfidont.plist.template" \
    > "$AGENT_DIR/com.vphone.amfidont.plist"

sed \
    -e "s|{{USER}}|$USER_NAME|g" \
    -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
    "$(dirname "$0")/vphone-amfidont.sudoers.template" \
    > "$AGENT_DIR/vphone-amfidont.sudoers"

plutil -lint "$AGENT_DIR/com.vphone.amfidont.plist" >/dev/null \
    && echo "plist OK" || { echo "error: plist failed validation" >&2; exit 1; }

cat <<EOF

Generated:
  $AGENT_DIR/com.vphone.amfidont.plist
  $AGENT_DIR/vphone-amfidont.sudoers

One-time install (run these):

  cp "$AGENT_DIR/com.vphone.amfidont.plist" "$HOME_DIR/Library/LaunchAgents/"
  sudo cp "$AGENT_DIR/vphone-amfidont.sudoers" /etc/sudoers.d/vphone-amfidont
  sudo chown root:wheel /etc/sudoers.d/vphone-amfidont
  sudo chmod 0440 /etc/sudoers.d/vphone-amfidont
  sudo visudo -c
  launchctl bootout gui/$(id -u)/com.vphone.amfidont 2>/dev/null || true
  launchctl bootstrap gui/$(id -u) "$HOME_DIR/Library/LaunchAgents/com.vphone.amfidont.plist"

Verify:

  launchctl print gui/$(id -u)/com.vphone.amfidont | grep state
  tail -2 "$LOG_PATH"
EOF
