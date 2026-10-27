#!/bin/bash
# Creates a compressed DMG containing QuotaCanvas.app and an Applications link.
# Requires a built app; does not install or launch it.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/build/QuotaCanvas.app"
[ -d "$APP" ] || { echo 'Build QuotaCanvas.app first: ./scripts/build.sh' >&2; exit 1; }
VERSION=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")
DMG="$ROOT/build/QuotaCanvas-$VERSION.dmg"
STAGE=$(mktemp -d "$ROOT/build/.dmg.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
mkdir "$STAGE/content"
# Copy without extended attributes; leftover xattrs break the strict signature check.
ditto --norsrc --noextattr --noqtn --noacl "$APP" "$STAGE/content/QuotaCanvas.app"
codesign --verify --deep --strict "$STAGE/content/QuotaCanvas.app"
ln -s /Applications "$STAGE/content/Applications"
hdiutil create -volname QuotaCanvas -srcfolder "$STAGE/content" -fs HFS+ -format UDZO -ov "$DMG"
echo "Built: $DMG"
