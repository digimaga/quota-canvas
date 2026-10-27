#!/bin/bash
# quota-canvas-routing: SessionStart hook for Claude Code / Codex.
# Runs `quota-canvas routing hook` once and lets its single JSON line become the
# additionalContext of the session start. Never gets in the way: whatever goes
# wrong (binary missing, engine off, non-macOS), stay silent and exit 0.

[ "$(uname -s)" = "Darwin" ] || exit 0

# Find the quota-canvas binary: $QUOTA_CANVAS_BIN, then the PATH, then the
# documented repository venv location.
bin="${QUOTA_CANVAS_BIN:-}"
if [ -z "$bin" ]; then
  bin="$(command -v quota-canvas 2>/dev/null || true)"
fi
if [ -z "$bin" ] && [ -n "$HOME" ]; then
  candidate="$HOME/dev/quota-canvas/.venv/bin/quota-canvas"
  if [ -x "$candidate" ]; then
    bin="$candidate"
  fi
fi
if [ -z "$bin" ] || [ ! -x "$bin" ]; then
  exit 0
fi

"$bin" routing hook </dev/null 2>/dev/null
exit 0
