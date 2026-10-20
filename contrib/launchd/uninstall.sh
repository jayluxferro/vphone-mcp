#!/bin/bash
# Remove the vphone-mcp LaunchAgent. The token file is left in place so a
# later re-install keeps the same token; pass --purge-token to delete it.
set -euo pipefail

LABEL="com.vphone.mcp"
UID_NUM="$(id -u)"
AGENT_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TOKEN_FILE="${VPHONE_ROOT:-$HOME/.vphone}/mcp-auth-token"

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
rm -f "$AGENT_PLIST"
echo "removed $AGENT_PLIST"

if [[ "${1:-}" == "--purge-token" ]]; then
    rm -f "$TOKEN_FILE"
    echo "removed $TOKEN_FILE — a re-install will mint a new token"
else
    echo "kept $TOKEN_FILE (--purge-token to delete it)"
fi
