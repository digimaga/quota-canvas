#!/bin/bash
# Pull new commits of this repo and restart the quota-canvas LaunchAgents.
# Run periodically by ./scripts/install-auto-update.sh (com.digimaga.quota-canvas-autoupdate).
# - fast-forward only; does nothing when the checkout is not on main, has
#   uncommitted changes, or has commits the remote does not have
# - reinstalls the package when pyproject.toml changed since the last application;
#   restarts anyway when that fails and retries the reinstall on later runs
# - restarts only the quota-canvas agents that are loaded on this Mac
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH=main
AGENTS=(com.digimaga.quota-canvas com.digimaga.quota-canvas-headless)
STATE_DIR="$HOME/Library/Application Support/quota-canvas"
repo_key="$(printf '%s' "$REPO" | shasum -a 256 | cut -d ' ' -f 1)"
STATE_FILE="$STATE_DIR/auto-update-$repo_key.sha"
PENDING_FILE="$STATE_DIR/auto-update-$repo_key.reinstall-pending"

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

# Reinstall the package into .venv. Returns 1 when no installer is found or the
# install fails, so the caller can still restart the agents (an editable install
# picks up source changes on restart) and retry the reinstall next time.
# .venv made by uv has no pip, and launchd's PATH may not include uv.
reinstall_package() {
    local uv_bin=""
    if [[ -x "$REPO/.venv/bin/pip" ]]; then
        "$REPO/.venv/bin/pip" install -q -e "$REPO" || { log "warn: reinstall failed (pip)"; return 1; }
        log "reinstalled package (pip)"
        return 0
    fi
    uv_bin="$(command -v uv 2>/dev/null || true)"
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
        [[ -n "$uv_bin" ]] && break
        [[ -x "$candidate" ]] && uv_bin="$candidate"
    done
    if [[ -n "$uv_bin" ]]; then
        "$uv_bin" pip install -q --python "$REPO/.venv/bin/python" -e "$REPO" ||
            { log "warn: reinstall failed (uv)"; return 1; }
        log "reinstalled package (uv)"
        return 0
    fi
    if "$REPO/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
        "$REPO/.venv/bin/python" -m pip install -q -e "$REPO" ||
            { log "warn: reinstall failed (python -m pip)"; return 1; }
        log "reinstalled package (python -m pip)"
        return 0
    fi
    log "warn: no pip or uv; skipped reinstall (run ./scripts/setup.sh)"
    return 1
}

# Restart the agents that are loaded on this Mac. Returns 1 when a kickstart
# fails or launchctl print fails for a reason other than "not loaded" (the
# restart may not have happened), so the caller can retry on the next run; an
# agent that is not loaded (launchctl print cannot find it) is skipped.
restart_agents() {
    local failed=0 print_out="" print_rc=0
    for label in "${AGENTS[@]}"; do
        if print_out="$(launchctl print "gui/$(id -u)/$label" 2>&1)"; then
            if launchctl kickstart -k "gui/$(id -u)/$label"; then
                log "restarted $label"
            else
                log "warn: restart failed (kickstart $label)"
                failed=1
            fi
        else
            print_rc=$?
            if [[ "$print_rc" == 113 ]] || [[ "$print_out" == *"Could not find service"* ]]; then
                log "skip restart: $label is not loaded"
            else
                log "warn: launchctl print $label failed (rc=$print_rc): ${print_out%%$'\n'*}"
                failed=1  # 再起動できたか分からないので成功にしない
            fi
        fi
    done
    return "$failed"
}

if [[ "$applied" == "$remote_head" ]]; then
    # Already applied. Retry only a reinstall that pyproject.toml needed but failed.
    # Keep the pending marker until the restart succeeds too: a failed kickstart
    # would otherwise be forgotten (SHA matches and no pending marker next run).
    if [[ -f "$PENDING_FILE" ]] && reinstall_package; then
        if restart_agents; then
            rm -f "$PENDING_FILE"
        else
            log "warn: keeping pending marker; retrying reinstall and restart next run"
            exit 1
        fi
    fi
    exit 0
fi

# A missing or unavailable baseline cannot prove dependencies are current.
reinstall=true
deps_changed=false
if [[ -n "$applied" ]] && git cat-file -e "$applied^{commit}" 2>/dev/null &&
   git merge-base --is-ancestor "$applied" "$remote_head"; then
    changed="$(git diff --name-only "$applied" "$remote_head")"
    if grep -qx 'pyproject.toml' <<<"$changed"; then
        deps_changed=true
    else
        reinstall=false
    fi
else
    deps_changed=true  # 基準が無い・使えない (初回実行・force push など) ので依存が変わったとみなす
fi
if [[ -f "$PENDING_FILE" ]]; then
    reinstall=true  # 前回入れ直せなかった分をこの回でやり直す
    deps_changed=true
fi
if "$reinstall"; then
    if reinstall_package; then
        rm -f "$PENDING_FILE"
    elif "$deps_changed"; then
        mkdir -p "$STATE_DIR"
        touch "$PENDING_FILE"
    fi
fi

restart_agents

mkdir -p "$STATE_DIR"
state_tmp="$(mktemp "$STATE_DIR/.auto-update.XXXXXX")"
trap 'rm -f "$state_tmp"' EXIT
printf '%s\n' "$remote_head" > "$state_tmp"
mv -f "$state_tmp" "$STATE_FILE"
