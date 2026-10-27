#!/bin/bash
# Install push_listener.py on this Mac (panel "Scan -> Computer" -> Epson ScanSmart) and start it at login.
# Optionally stop Epson Event Manager / Scanner Monitor from launching (they are Intel-only and cannot run
# on Apple silicon without Rosetta). ScanSmart refuses panel scans ("required files not found") unless
# /Applications/Epson Software/Event Manager.app exists, so an empty placeholder is created if it is missing.
#
# Usage: bash install.sh [--disable-event-manager] [--scanner "EPSON L6270 Series"] [name shown on the panel]
set -euo pipefail

usage() {
    echo "Usage: bash install.sh [--disable-event-manager] [--scanner \"EPSON L6270 Series\"] [panel name]"
}

DISABLE_EM=0
SCANNER=""
NAME=""
while [ $# -gt 0 ]; do
    case "$1" in
        --disable-event-manager) DISABLE_EM=1 ;;
        --scanner) SCANNER="${2:?--scanner needs a value}"; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) usage >&2; exit 1 ;;
        *) NAME="$1" ;;
    esac
    shift
done

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.epson-pushscan"
LEGACY_LABEL="com.snaken.epson-pushscan"  # label used by earlier versions of this script
DEST="$HOME/Library/Application Support/epson-pushscan"  # not under Documents/Downloads, which launchd agents may not read
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/epson-pushscan.log"
UID_NUM="$(id -u)"

# Find Python 3.9+ (Homebrew first, then /usr/bin/python3 from the Xcode Command Line Tools)
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -x "$c" ] || continue
    [ "$c" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1 && continue
    if "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
        PY="$("$c" -c 'import sys; print(sys.executable)')"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.9 or newer not found. Run xcode-select --install first, then run this script again." >&2
    exit 1
fi

# The panel only reliably shows ASCII; fall back to LocalHostName if ComputerName is not ASCII
[ -n "$NAME" ] || NAME="$(scutil --get ComputerName)"
if ! printf '%s' "$NAME" | LC_ALL=C grep -q '^[ -~]*$'; then
    NAME="$(scutil --get LocalHostName)"
fi

if [ ! -d "/Applications/Epson Software/Epson ScanSmart.app" ]; then
    echo "Note: Epson ScanSmart is not installed, so nothing will happen when Start is pressed. Install it from Epson's website first." >&2
fi

mkdir -p "$DEST" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$HERE/push_listener.py" "$DEST/"

SCANNER_ARGS=""
if [ -n "$SCANNER" ]; then
    SCANNER_ARGS="<string>--scanner</string><string>$SCANNER</string>"
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DEST/push_listener.py</string>
    <string>--name</string>
    <string>$NAME</string>
    $SCANNER_ARGS
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null

launchctl bootout "gui/$UID_NUM/$LEGACY_LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LEGACY_LABEL.plist"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
echo "Installed: the panel will list \"${NAME}\". Log: ${LOG}"

# Epson Event Manager / Scanner Monitor (Intel-only). Their launch agents live in system locations, so
# changing them needs an administrator password. ScanSmart only checks that Event Manager.app exists.
EM_APP="/Applications/Epson Software/Event Manager.app"
EM_PLIST="/Library/LaunchAgents/com.epson.eventmanager.agent.plist"
SM_PLIST="/Library/LaunchAgents/com.epson.scannermonitor.plist"
if [ ! -e "$EM_APP" ]; then
    echo "Creating an empty $EM_APP (ScanSmart will not scan from the panel without it);"
    echo "macOS will ask for an administrator password:"
    osascript -e "do shell script \"mkdir -p '$EM_APP'\" with administrator privileges" >/dev/null \
        || echo "Could not create it. Create it manually: sudo mkdir \"$EM_APP\"" >&2
fi
if [ -e "$EM_PLIST" ] || [ -e "$SM_PLIST" ]; then
    if [ "$DISABLE_EM" = 1 ]; then
        echo "Disabling Epson Event Manager / Scanner Monitor launch agents; macOS will ask for an administrator password:"
        launchctl bootout "gui/$UID_NUM/com.epson.eventmanager.agent" 2>/dev/null || true
        launchctl bootout "gui/$UID_NUM/com.epson.scannermonitor" 2>/dev/null || true
        CMD="rm -f '$EM_PLIST' '$SM_PLIST'"
        osascript -e "do shell script \"$CMD\" with administrator privileges" >/dev/null
        echo "Disabled."
    else
        echo "Note: Epson Event Manager's launch agent is still installed. Without Rosetta it cannot start and does no"
        echo "harm; if it does run, it competes for port 2968. Run again with --disable-event-manager to turn it off."
    fi
fi
