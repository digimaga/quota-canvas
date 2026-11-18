#!/bin/sh
set -eu

case "${1-}" in
  "") dry_run=0 ;;
  --dry-run) dry_run=1 ;;
  *) printf 'Usage: %s [--dry-run]\n' "$0" >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { printf 'Usage: %s [--dry-run]\n' "$0" >&2; exit 2; }
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target_dir=${HOME}/.pi/agent/extensions
target=$target_dir/ds-gate-lease.ts
if [ "$dry_run" -eq 1 ]; then
  printf 'コピー先: %s\n' "$target"
  if [ -e "$target" ]; then printf '既存ファイルはバックアップしてから配置します\n'; fi
  exit 0
fi
mkdir -p "$target_dir"
if [ -e "$target" ]; then
  backup=$target.bak
  n=0
  while [ -e "$backup" ]; do n=$((n + 1)); backup=$target.bak.$n; done
  mv -- "$target" "$backup"
  printf '既存ファイルを退避しました: %s\n' "$backup"
fi
cp -- "$source_dir/ds-gate-lease.ts" "$target"
printf 'インストールしました: %s\n' "$target"
