#!/bin/bash
# Run `quota-canvas run` at login and keep it running (LaunchAgent, no sudo).
# Headless: ./scripts/install-agent.sh --no-screen
# Uninstall: ./scripts/install-agent.sh [--no-screen] --uninstall
set -euo pipefail

LABEL=com.digimaga.quota-canvas
headless=false
uninstall=false
for arg in "$@"; do
    case "$arg" in
        --no-screen) headless=true ;;
        --uninstall) uninstall=true ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done
if "$headless"; then LABEL="$LABEL-headless"; fi
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOGDIR="$HOME/Library/Logs/quota-canvas"

if "$uninstall"; then
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
headless_arg=""
out_log=out.log
err_log=err.log
if "$headless"; then
    headless_arg='<string>--no-screen</string>'
    out_log=headless.out.log
    err_log=headless.err.log
fi
cat > "$temp_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$repo_xml/.venv/bin/quota-canvas</string>
        $headless_arg
        <string>run</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardOutPath</key><string>$log_xml/$out_log</string>
    <key>StandardErrorPath</key><string>$log_xml/$err_log</string>
</dict>
</plist>
EOF
plutil -lint "$temp_plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
mv -f "$temp_plist" "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL (logs: $LOGDIR)"
