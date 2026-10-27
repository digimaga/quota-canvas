#!/bin/bash
# Pull new commits of this repo and restart the quota-canvas LaunchAgents.
# Run periodically by ./scripts/install-auto-update.sh (com.digimaga.quota-canvas-autoupdate).
# - fast-forward only; does nothing when the checkout is not on main, has
#   uncommitted changes, or has commits the remote does not have
# - reinstalls the package only when pyproject.toml changed
# - restarts only the quota-canvas agents that are loaded on this Mac
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH=main
AGENTS=(com.digimaga.quota-canvas com.digimaga.quota-canvas-headless)

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') $*"; }

cd "$REPO"
[[ "$(git branch --show-current)" == "$BRANCH" ]] || { log "skip: not on $BRANCH"; exit 0; }
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { log "skip: uncommitted changes"; exit 0; }

git fetch -q origin "$BRANCH"
local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse "origin/$BRANCH")"
[[ "$local_head" != "$remote_head" ]] || exit 0
git merge-base --is-ancestor HEAD "origin/$BRANCH" || { log "skip: local commits not on origin/$BRANCH"; exit 0; }

changed="$(git diff --name-only "$local_head" "$remote_head")"
git merge -q --ff-only "origin/$BRANCH"
log "updated ${local_head:0:7} -> ${remote_head:0:7}"

if grep -qx 'pyproject.toml' <<<"$changed"; then
    "$REPO/.venv/bin/pip" install -q -e "$REPO"
    log "reinstalled package (pyproject.toml changed)"
fi

for label in "${AGENTS[@]}"; do
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        launchctl kickstart -k "gui/$(id -u)/$label"
        log "restarted $label"
    fi
done
