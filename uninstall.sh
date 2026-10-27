#!/bin/bash
# Stop push_listener.py and remove what install.sh put on this Mac (the log file is kept).
set -euo pipefail

UID_NUM="$(id -u)"
for LABEL in local.epson-pushscan com.snaken.epson-pushscan; do
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
done
rm -rf "$HOME/Library/Application Support/epson-pushscan"
echo "Uninstalled. The log is still at $HOME/Library/Logs/epson-pushscan.log"
