#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
"$ROOT/scripts/check-localizations.py"
"$ROOT/scripts/check-ui-regressions.py"
sources=()
while IFS= read -r file; do sources+=("$file"); done < <(find "$ROOT/Sources/QuotaCanvasApp" -name '*.swift' ! -name QuotaCanvasApp.swift -print)
swiftc -module-cache-path "$TMP_DIR/module-cache" -parse-as-library -swift-version 5 "${sources[@]}" "$ROOT/scripts/RegressionTests.swift" -o "$TMP_DIR/regression-tests" -framework AppKit -framework ServiceManagement -framework UserNotifications
QUOTA_CANVAS_TEST_REPO="$ROOT/.." "$TMP_DIR/regression-tests"
