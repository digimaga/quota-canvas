#!/bin/sh
set -eu
case "${1-}" in
  '') dry_run=0 ;;
  --dry-run) dry_run=1 ;;
  *) echo 'Usage: install.sh [--dry-run]' >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { echo 'Usage: install.sh [--dry-run]' >&2; exit 2; }
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target_dir=${HOME}/.config/opencode/plugins
target=$target_dir/quota-guard.js
if [ -e "$target" ] && cmp -s "$source_dir/quota-guard.js" "$target"; then
  echo 'quota-guard.js は既に同じ内容です。'
  exit 0
fi
if [ -e "$target" ] && [ -e "$target.bak" ]; then
  echo '退避先 quota-guard.js.bak が既に存在します。' >&2
  exit 1
fi
if [ "$dry_run" -eq 1 ]; then
  [ ! -e "$target" ] || echo '既存の quota-guard.js を quota-guard.js.bak に退避します。'
  echo 'quota-guard.js を OpenCode plugins に配置します。'
  exit 0
fi
mkdir -p "$target_dir"
if [ -e "$target" ]; then
  cp -p "$target" "$target.bak"
fi
cp "$source_dir/quota-guard.js" "$target"
echo 'quota-guard.js を配置しました。OpenCode を再起動してください。'
