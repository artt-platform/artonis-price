#!/usr/bin/env bash
# Install the launchd LaunchAgent that pulls Invaluable hammer prices
# on Mon + Thu at 10:00.  Run once.  Re-run to update after editing
# the plist.

set -euo pipefail

PLIST_NAME="com.artonis.pull_hammers"
PLIST_FILE="$(cd "$(dirname "$0")" && pwd)/${PLIST_NAME}.plist"
TARGET="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

if [ ! -f "$PLIST_FILE" ]; then
    echo "✗ Plist not found at $PLIST_FILE" >&2
    exit 1
fi

# Unload any previous version
launchctl unload "$TARGET" 2>/dev/null || true

# Copy the plist into ~/Library/LaunchAgents
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_FILE" "$TARGET"

# Load it — registers with launchd
launchctl load "$TARGET"

echo "✓ Installed ${PLIST_NAME}"
echo ""
echo "Next runs:"
echo "  Mon 10:00 — Invaluable hammer pull (max 10 lots)"
echo "  Thu 10:00 — same"
echo ""
echo "If your Mac is asleep at that time, launchd queues the run"
echo "and fires it on the next wake.  No action needed."
echo ""
echo "Manage:"
echo "  Disable:   launchctl unload $TARGET"
echo "  Re-enable: launchctl load $TARGET"
echo "  Status:    launchctl list | grep ${PLIST_NAME}"
echo "  Logs:      tail -f /tmp/artonis_pull_hammers.log"
echo "  Errors:    tail -f /tmp/artonis_pull_hammers.err"
echo ""
echo "Force a test run NOW (don't wait for schedule):"
echo "  launchctl start ${PLIST_NAME}"
