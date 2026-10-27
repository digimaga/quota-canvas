#!/bin/bash
# Pull new commits of this repo and restart the quota-canvas LaunchAgents.
# Run periodically by ./scripts/install-auto-update.sh (com.digimaga.quota-canvas-autoupdate).
# - fast-forward only; does nothing when the checkout is not on main, has
#   uncommitted changes, or has commits the remote does not have
# - reinstalls the package when pyproject.toml changed since the last application
# - restarts only the quota-canvas agents that are loaded on this Mac
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH=main
AGENTS=(com.digimaga.quota-canvas com.digimaga.quota-canvas-headless)
STATE_DIR="$HOME/Library/Application Support/quota-canvas"
repo_key="$(printf '%s' "$REPO" | shasum -a 256 | cut -d ' ' -f 1)"
STATE_FILE="$STATE_DIR/auto-update-$repo_key.sha"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') $*"; }

cd "$REPO"
[[ "$(git branch --show-current)" == "$BRANCH" ]] || { log "skip: not on $BRANCH"; exit 0; }
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { log "skip: uncommitted changes"; exit 0; }

git fetch -q origin "$BRANCH"
local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse "origin/$BRANCH")"
if [[ "$local_head" != "$remote_head" ]]; then
    git merge-base --is-ancestor HEAD "origin/$BRANCH" || { log "skip: local commits not on origin/$BRANCH"; exit 0; }
    git merge -q --ff-only "origin/$BRANCH"
    log "updated ${local_head:0:7} -> ${remote_head:0:7}"
fi

applied=""
if [[ -f "$STATE_FILE" ]]; then
    applied="$(cat "$STATE_FILE")"
fi
[[ "$applied" != "$remote_head" ]] || exit 0

# A missing or unavailable baseline cannot prove dependencies are current.
reinstall=true
if [[ -n "$applied" ]] && git cat-file -e "$applied^{commit}" 2>/dev/null &&
   git merge-base --is-ancestor "$applied" "$remote_head"; then
    changed="$(git diff --name-only "$applied" "$remote_head")"
    if ! grep -qx 'pyproject.toml' <<<"$changed"; then
        reinstall=false
    fi
fi
if "$reinstall"; then
    "$REPO/.venv/bin/pip" install -q -e "$REPO"
    log "reinstalled package"
fi

for label in "${AGENTS[@]}"; do
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        launchctl kickstart -k "gui/$(id -u)/$label"
        log "restarted $label"
    fi
done

mkdir -p "$STATE_DIR"
state_tmp="$(mktemp "$STATE_DIR/.auto-update.XXXXXX")"
trap 'rm -f "$state_tmp"' EXIT
printf '%s\n' "$remote_head" > "$state_tmp"
mv -f "$state_tmp" "$STATE_FILE"
