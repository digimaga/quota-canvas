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
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .

echo "Done. Try: .venv/bin/turzx test"
