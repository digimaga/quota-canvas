#!/bin/sh
set -eu

mode=install
dry_run=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        --claude-hook)
            [ "$mode" = install ] || { printf '%s\n' 'オプションを併用できません' >&2; exit 2; }
            mode=add-hook ;;
        --remove-claude-hook)
            [ "$mode" = install ] || { printf '%s\n' 'オプションを併用できません' >&2; exit 2; }
            mode=remove-hook ;;
        *) printf '%s\n' '使い方: install.sh [--dry-run] [--claude-hook|--remove-claude-hook]' >&2; exit 2 ;;
    esac
done
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destination="$HOME/.local/bin/orca-agent-gate"
settings=${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}
if [ "$dry_run" -eq 1 ]; then
    if [ "$mode" = install ]; then
        printf 'インストール予定: %s -> %s\n' "$source_dir/orca-agent-gate" "$destination"
    else
        printf 'Claude hook %s 予定: %s\n' "$( [ "$mode" = add-hook ] && printf '追加' || printf '削除' )" "$settings"
    fi
    exit 0
fi

if [ "$mode" = install ]; then
    mkdir -p "$HOME/.local/bin"
    install -m 755 "$source_dir/orca-agent-gate" "$destination"
    printf 'インストールしました: %s\n' "$destination"
    exit 0
fi

python3 - "$settings" "$mode" <<'PY'
import fcntl, json, os, stat, sys, tempfile, time

path, mode = sys.argv[1:]
path = os.path.realpath(path)
directory = os.path.dirname(os.path.abspath(path))
os.makedirs(directory, exist_ok=True)
try:
    lock_fd = os.open(path + ".orca-gate.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
except OSError as exc:
    print("設定のロックファイルを開けません: {}".format(exc), file=sys.stderr)
    sys.exit(1)
if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
    os.close(lock_fd)
    print("設定のロックファイルは通常ファイルである必要があります", file=sys.stderr)
    sys.exit(1)
# 既存のロックの権限は変えない（ハードリンクで共有された inode を変えないため）。
# 新規作成時の 0600 は os.open の mode で付く
deadline = time.monotonic() + 3
while True:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic() >= deadline:
            print("設定のロックを取得できませんでした。もう一度実行してください", file=sys.stderr)
            sys.exit(1)
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
command = '[ -n "$ORCA_PANE_KEY" ] && [ "${ORCA_GATE_HOOK:-}" != off ] || exit 0; exec "$HOME/.local/bin/orca-agent-gate" hook claude'
legacy_commands = {os.path.expanduser('~/.local/bin/orca-agent-gate') + ' hook claude',
                   '$HOME/.local/bin/orca-agent-gate hook claude'}
owned_commands = legacy_commands | {command}
try:
    with open(path, "rb") as stream:
        original = stream.read()
        original_mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
except FileNotFoundError:
    original = None
    original_mode = 0o600
except OSError as exc:
    print("settings.json を読み込めません: {}".format(exc), file=sys.stderr)
    sys.exit(1)
try:
    data = json.loads(original) if original is not None else {}
except (UnicodeError, json.JSONDecodeError) as exc:
    print("settings.json を読み込めません: {}".format(exc), file=sys.stderr)
    sys.exit(1)
if not isinstance(data, dict):
    print("settings.json のトップレベルは object である必要があります", file=sys.stderr)
    sys.exit(1)

hooks = data.get("hooks")
if hooks is None:
    hooks = {}
elif not isinstance(hooks, dict):
    print("settings.json の hooks は object である必要があります", file=sys.stderr)
    sys.exit(1)
entries = hooks.get("UserPromptSubmit")
if entries is None:
    entries = []
elif not isinstance(entries, list):
    print("settings.json の UserPromptSubmit は array である必要があります", file=sys.stderr)
    sys.exit(1)

if mode == "add-hook":
    changed = False
    found = False
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            continue
        for hook in entry["hooks"]:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str) and hook["command"] in owned_commands:
                found = True
                if hook["command"] != command:
                    hook["command"] = command
                    changed = True
    if not found:
        entries.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
        changed = True
else:
    filtered = []
    changed = False
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            filtered.append(entry)
            continue
        group = [hook for hook in entry["hooks"]
                 if not (isinstance(hook, dict) and isinstance(hook.get("command"), str)
                         and hook["command"] in owned_commands)]
        changed = changed or len(group) != len(entry["hooks"])
        if group:
            copy = dict(entry)
            copy["hooks"] = group
            filtered.append(copy)
    entries = filtered
if not changed:
    sys.exit(0)
if entries:
    hooks["UserPromptSubmit"] = entries
else:
    hooks.pop("UserPromptSubmit", None)
if hooks:
    data["hooks"] = hooks
else:
    data.pop("hooks", None)

updated = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
if original is not None:
    backup_fd, backup = tempfile.mkstemp(prefix=os.path.basename(path) + ".bak-orca-gate-", dir=directory)
    try:
        with os.fdopen(backup_fd, "wb") as stream:
            stream.write(original)
    except BaseException:
        os.unlink(backup)
        raise
fd, temporary = tempfile.mkstemp(prefix=".settings.json.", dir=directory)
try:
    with os.fdopen(fd, "wb") as stream:
        stream.write(updated)
    os.chmod(temporary, original_mode)
    try:
        with open(path, "rb") as stream:
            current = stream.read()
            current_mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
    except FileNotFoundError:
        current = None
        current_mode = None
    if current != original or (original is not None and current_mode != original_mode):
        print("設定が別のプロセスで更新されました。もう一度実行してください", file=sys.stderr)
        sys.exit(1)
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
printf 'Claude hook を%sしました: %s\n' "$( [ "$mode" = add-hook ] && printf '追加' || printf '削除' )" "$settings"
