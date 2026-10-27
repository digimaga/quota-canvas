#!/bin/bash
# Run `quota-canvas run` at login and keep it running (LaunchAgent, no sudo).
# Uninstall: ./scripts/install-agent.sh --uninstall
set -euo pipefail

LABEL=com.digimaga.quota-canvas
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
        <string>$REPO/.venv/bin/quota-canvas</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardOutPath</key><string>$LOGDIR/out.log</string>
    <key>StandardErrorPath</key><string>$LOGDIR/err.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL (logs: $LOGDIR)"
