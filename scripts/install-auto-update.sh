#!/bin/bash
# Run ./scripts/auto-update.sh every 10 minutes (LaunchAgent, no sudo), so this
# Mac picks up new commits on main and restarts its quota-canvas agents.
# Uninstall: ./scripts/install-auto-update.sh --uninstall
set -euo pipefail

LABEL=com.digimaga.quota-canvas-autoupdate
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOGDIR="$HOME/Library/Logs/quota-canvas"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$PLIST"
    echo "removed $LABEL"
    exit 0
fi

mkdir -p "$LOGDIR" "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$REPO/scripts/auto-update.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>StartInterval</key><integer>600</integer>
    <key>StandardOutPath</key><string>$LOGDIR/autoupdate.log</string>
    <key>StandardErrorPath</key><string>$LOGDIR/autoupdate.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL (every 10 min, log: $LOGDIR/autoupdate.log)"
