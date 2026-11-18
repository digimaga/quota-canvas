#!/bin/sh
set -eu
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--dry-run" ]; }; then
    printf '%s\n' '使い方: install.sh [--dry-run]' >&2
    exit 2
fi
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destination="$HOME/.local/bin/orca-agent-gate"
if [ "${1:-}" = "--dry-run" ]; then
    printf 'インストール予定: %s -> %s\n' "$source_dir/orca-agent-gate" "$destination"
    exit 0
fi
mkdir -p "$HOME/.local/bin"
install -m 755 "$source_dir/orca-agent-gate" "$destination"
printf 'インストールしました: %s\n' "$destination"
