#!/bin/bash
# Set up the TURZX screen tool: libusb (Homebrew) + .venv with this package installed.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! brew list libusb >/dev/null 2>&1; then
  brew install libusb
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

# .venv made by uv has no pip, so pip is not required: prefer it when present,
# then uv (explicit candidates because a non-login shell may lack it in PATH),
# then `python -m pip`. Same order as reinstall_package in auto-update.sh.
install_package() {
  local uv_bin="" candidate
  if [ -x .venv/bin/pip ]; then
    .venv/bin/pip install -q --upgrade pip || return 1
    .venv/bin/pip install -q -e . || return 1
    return 0
  fi
  uv_bin="$(command -v uv 2>/dev/null || true)"
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    [ -n "$uv_bin" ] && break
    [ -x "$candidate" ] && uv_bin="$candidate"
  done
  if [ -n "$uv_bin" ]; then
    "$uv_bin" pip install -q --python .venv/bin/python -e . || return 1
    return 0
  fi
  if .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m pip install -q -e . || return 1
    return 0
  fi
  return 1
}

if ! install_package; then
  echo "Could not install this package: .venv/bin/pip is missing and neither uv" >&2
  echo "nor a pip module is available. Install uv or recreate .venv with" >&2
  echo "'python3 -m venv .venv' and run this script again." >&2
  exit 1
fi

echo "Done. Try: .venv/bin/turzx test"
