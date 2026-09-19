#!/bin/sh
# Stop the always-on server and remove the launch agent. Data and settings are untouched.
# Removes the current label and the legacy one (used before the project was generalised), so an
# old install never keeps a second server on the same port.
set -u

DOMAIN="gui/$(id -u)"
AGENTS_DIR="$HOME/Library/LaunchAgents"

for label in com.salescoach.serve com.ordinal.salescoach; do
    was=""
    if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
        launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
        was="stopped"
    fi
    if [ -f "$AGENTS_DIR/$label.plist" ]; then
        rm -f "$AGENTS_DIR/$label.plist"
        was="${was:+$was and }removed"
    fi
    echo "$label: ${was:-not installed}"
done
