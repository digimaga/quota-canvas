#!/bin/bash
# Builds QuotaCanvasApp in release mode and assembles build/QuotaCanvas.app
# (menu-bar only app, ad-hoc signed). Requires only the Command Line Tools.
# Does not install or launch anything.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

swift build -c release

BIN_DIR="$(swift build -c release --show-bin-path)"
APP="$ROOT/build/QuotaCanvas.app"
CONTENTS="$APP/Contents"

rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

cp "$BIN_DIR/QuotaCanvasApp" "$CONTENTS/MacOS/QuotaCanvasApp"
cp "$ROOT/Packaging/Info.plist" "$CONTENTS/Info.plist"
printf 'APPL????' > "$CONTENTS/PkgInfo"

# Localizations (ja = development language, keys are the Japanese text itself)
for lproj in "$ROOT"/Resources/*.lproj; do
	[ -d "$lproj" ] || continue
	cp -R "$lproj" "$CONTENTS/Resources/"
done

# Signing: ad-hoc. The app holds no TCC grants that must survive rebuilds.
codesign --force --deep -s - "$APP"

echo
echo "Built: $APP"
codesign -dv "$APP" 2>&1 | grep -E "^(Identifier|Signature)=" || true
