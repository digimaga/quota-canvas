#!/bin/bash
# Run ./scripts/auto-update.sh every 10 minutes (LaunchAgent, no sudo), so this
# Mac picks up new commits on main and restarts its quota-canvas agents.
# Uninstall: ./scripts/install-auto-update.sh --uninstall
set -euo pipefail

LABEL=com.digimaga.quota-canvas-autoupdate
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOGDIR="$HOME/Library/Logs/quota-canvas"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL"
    exit 0
fi

mkdir -p "$LOGDIR" "$(dirname "$PLIST")"
xml_escape() {
    printf '%s' "$1" | sed -e 's/\&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}
repo_xml="$(xml_escape "$REPO")"
log_xml="$(xml_escape "$LOGDIR")"
temp_plist="$(mktemp "$(dirname "$PLIST")/.${LABEL}.XXXXXX")"
trap 'rm -f "$temp_plist"' EXIT
cat > "$temp_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$repo_xml/scripts/auto-update.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>StartInterval</key><integer>600</integer>
    <key>StandardOutPath</key><string>$log_xml/autoupdate.log</string>
    <key>StandardErrorPath</key><string>$log_xml/autoupdate.log</string>
</dict>
</plist>
EOF
plutil -lint "$temp_plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
mv -f "$temp_plist" "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL (every 10 min, log: $LOGDIR/autoupdate.log)"
