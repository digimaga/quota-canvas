# Orca Agent Gate

Orca がエージェントの CLI を起動する前に、残量と任意の同時起動数を確認するラッパーです。Python 3.9 以降と標準ライブラリを使います。入力・出力・TTY を引き継ぎ、モデル指定には下記の規則を適用します。

## 導入

```sh
./integrations/orca/install.sh --dry-run
./integrations/orca/install.sh
```

`~/.local/bin/orca-agent-gate` にコピーします。Orca の Settings → Agents → Command に、各エージェントの ID を固定の前置引数として設定してください。例えば `codex` なら次の形です。

```text
$HOME/.local/bin/orca-agent-gate codex
```

Command が環境変数を展開しない場合もあるため、**`$HOME` を実際のホームディレクトリに展開した絶対パス**を入力してください。`claude`、`opencode`、`opencode2`、`command-code`、`grok`、`devin`、`pi` も同様です。Orca はその後ろに必要な引数を追加します。本物の CLI が PATH から見つからない場合は設定の `cli` に絶対パスを指定します。PATH 検索ではラッパー自身を除き、`opencode2` の本物は `opencode` として探します。

## 設定

`~/.config/orca-agent-gate/config.json` がない場合は既定値を使います。設定例:

```json
{
  "agents": {
    "codex": {"worker": "codex", "cli": "/absolute/path/to/codex", "cap": 2},
    "opencode2": {"worker": "opencode", "cap": 1, "board_keys": ["swe2", "swe2f"]},
    "pi": {"worker": null},
    "grok": {"off": true}
  },
  "devin_model": "swe-2-high",
  "devin_allowed_models": ["swe-2-high", "swe-2-medium", "swe-2-max", "swe-2-flash-high", "swe-2-flash-medium", "swe-2-flash-max"]
}
```

`worker` は判定・同時本数の単位、`cli` は実行可能ファイルの絶対パス、`cap` はその worker の 1 以上の同時本数です。省略時は cap なし。複数 agent が worker を共有する場合は、設定された最小の cap と `board_keys` の和集合を使います。agent ID は英小文字・数字・ハイフンのみです。`off` は JSON の `true` のときだけ有効で、真偽値以外は警告して無効として扱います。`off: true` または `worker: null` は残量・同時本数の判定を無効にします。既定 worker は `claude → claude`、`codex → codex`、`opencode` と `opencode2 → opencode`、`command-code → commandcode`、`grok → grok`、`devin → swe2`、`pi` と未知の ID は判定なしです。

OpenDesign Go を使う場合は、opencode 側で provider `opendesign` とモデル `opendesign/deepseek-v4.1-flash` を設定し、必要な agent にキー取得を指定できます（既定では何もしません）。

```json
{"agents": {"opencode": {"standalone": true, "opendesign_key": {"worker": "opendesign", "command": ["/path/to/print-key"]}}}}
```

OpenCode 2.x は既定で常駐サービス（`opencode serve --service`）につなぐため、起動時に渡した環境変数（このキーや `ORCA_PANE_KEY`）とプラグインはそのままでは効きません。`standalone: true` にすると、対話起動（サブコマンドなし・ディレクトリ指定）と `run`・`mini` のときだけ先頭に `--standalone` を足し、起動ごとに専用のサーバーを立てます。`models`・`service`・`serve` などの管理用サブコマンドと、`--help`・`-h`・`--version`・`-v`・`--wizard`・`--completions` には足しません。最初の `--` より前に `--standalone`・`--no-standalone`・`--standalone=値`・`--server URL`・`--server=URL` があるときも足しません。`--server` で既存のサーバーにつなぐ場合、起動時の環境変数（キーや `ORCA_PANE_KEY`）はそのサーバーに届きません。真偽値以外は警告して無効として扱います。

OpenDesign のキーは、専用サーバーで動く起動（`standalone: true` で `--standalone` を足したとき、または自分で `--standalone` を付けた対話起動・`run`・`mini`）にだけ渡します。常駐サービスにつなぐ起動・`--server`・管理用サブコマンド（`serve --service` などで常駐サービスを立てる場合を含む）では取得コマンドを呼ばず、起動元の環境に既にある `OPENDESIGN_API_KEY` も外して、`launch.log` に `opendesign=off(shared-server)` と記録します。常駐サービスが起動元の環境を受け継いでキーを持ち続けないようにするためです。

agent の既定 worker `opencode` が `stop` の場合は、キー取得より前に起動が止まります。起動ゲートを外すには `worker: null` を指定してください。provider ごとの判定は `integrations/opencode` のプラグインと `opencode_providers` の対応表で行います。

既存の `OPENDESIGN_API_KEY` は優先されます。未設定時は指定 worker の判定が `stop` でなければ、または `allow` 中なら、コマンドをシェルなしで実行し、標準出力の1行目を起動先の環境変数に渡します。出力は最大 8 KiB、キーの1行目は最大 1024 文字で、超過時は取得失敗として扱います。15秒でタイムアウトし、取得失敗・空・停止判定でも CLI は通常どおり起動します。結果だけを `launch.log` の `opendesign=...` に記録し、キーやコマンドは記録しません。`command` の argv に秘密値を置かないでください。キーは opencode とその子プロセス（opencode が起動するツール）の環境に入ります。

判定コマンドは `ORCA_GATE_CHECK_CMD`、PATH または `~/.claude/bin` の `quota-gate check`、PATH の `quota-canvas routing check` の順に選び、末尾に worker を渡します。`ORCA_GATE_CHECK_CMD` はコマンドと固定引数を空白区切りで指定します（引用符を使えます）。exit 3 は停止、exit 0 の出力は注意として表示します。コマンドがない、10 秒で終了しない、その他の異常時は判定不能を通知して通常どおり起動します。タイムアウト時は判定コマンドのプロセスグループを終了します。判定コマンドの出力は一時ファイルで受けるため、出力が多くても判定は止まりません。CLI がラッパーへ `exec` で転送して同じ PID で再入した場合は停止します。転送スクリプトが子プロセスとしてラッパーを呼ぶ場合は検出できないので、`cli` には実体の実行ファイルの絶対パスを指定してください。

cap を設定すると `~/.local/state/orca-agent-gate/<worker>/<pid>` に起動時刻を保存します。次回の起動時に終了済み PID や再利用された PID を掃除します。同時本数の確認と PID 登録は worker ごとのロックで直列化します。任意の `board_keys` に文字列の配列を指定すると、`~/.claude/run/worker-board.json` の `total` 内の該当キーを合算します。`updated_at` が現在から 600 秒以内の JSON のみ有効です。ファイルがない、壊れている、古い場合は足しません。`ORCA_GATE_BOARD` で掲示板のパスを変更できます。掲示板と PID 記録が同じ起動を表す場合は重複して数えます。

Devin は `--model` がないとき `devin_model`（既定 `swe-2-high`）を追加します。指定済みモデルは `devin_allowed_models`（上記の6種類が既定）で照合し、許可外なら停止します。`acp` サブコマンドの場合は追加する `--model` を `acp` の直後に置きます。値のない `--model` は強制起動時も拒否します。`--model=value` 形式にも対応し、`--` 以降はプロンプトとして保持します。`off` はモデルの検査を無効化しません。

Command Code はモデル指定がない場合、UTC 平日の 01:00〜04:00 と 06:00〜10:00（終了時刻を除く）に `z-ai/glm-5.3-flash`、それ以外は `deepseek/deepseek-v4.1-flash` を追加します。強制起動時も同じです。`-m MODEL`、`-mMODEL`、`--model MODEL`、`--model=MODEL` を指定済みとみなし、`--` 以降は判定しません。先頭引数が `acp`、`mcp` などのサブコマンドなら自動追加しません。
`agents.command-code.auto_model` を `false` にすると無効になります。試験時は `ORCA_GATE_NOW=YYYY-MM-DDTHH:MM` で UTC の時刻を指定できます。不正な値は無視します。
自動追加したモデルは `launch.log` に記録します。起動後にユーザーが `/model` で切り替えた分は見ません。

## 一時的な回避と限界

一時的に起動時の残量・cap・Devin のモデル拒否を回避するには `ORCA_GATE_FORCE=1` を指定します。環境変数は CLI に引き継がず、強制起動したプロセスは同時本数に含まれません。恒常的に残量・cap 判定を無効化するには対象 agent に `"off": true` を設定します。

起動ごとに `~/.local/state/orca-agent-gate/launch.log` に時刻・agent・worker・判定・決定を追記します。引数やプロンプトは保存しません。ログは自動ローテーションしません。

これは Orca が設定された Command を起動するときだけ有効です。Orca のチャット型の一部や保存セッション復元が Command を通らない場合、また Devin の画面内でのモデル切替は止められません。直接 CLI を実行すれば回避できます。PID の起動時刻は `ps -o lstart=` に依存し、同じ秒に PID が再利用された場合は区別できません。

## 実行中の再判定

Claude Code の `UserPromptSubmit` フックから再判定できます。`./integrations/orca/install.sh --claude-hook` で導入し、`./integrations/orca/install.sh --remove-claude-hook` で外します。旧形式のフックも導入時に更新し、削除時に取り除きます。設定先は `CLAUDE_SETTINGS` があればそのファイル、なければ `$HOME/.claude/settings.json` です。シンボリックリンクは残してリンク先を更新し、更新前の内容を一意の名前のバックアップに保存します。バックアップと新規設定は mode `0600`、既存設定は元の mode を保ちます。更新直前の再読込で内容の変更を検出した場合は更新せず、再実行を求めます。`--dry-run` は書き込みません。

- `orca-agent-gate hook claude` はフック用です。Orca 外や `ORCA_GATE_HOOK=off` の場合はシェル内で終了し、ゲートを起動しません。
- `orca-agent-gate verdict <worker>` は現在の判定を1行JSONで表示します。
- `orca-agent-gate allow <worker> [分|off]` は指定 worker の stop を一時的に通します。期間の既定値は60分、最大720分です。

判定が `stop` の場合は次の入力を止めます（終了中のターンや会話は残ります）。`reserve` と判定不能は変化時に一度だけ注意を表示し、`go` と `boost` は無言です。使い切り期限を過ぎた判定は `reserve` として扱います。`ORCA_GATE_FORCE=1` で起動したセッションでは、起動時のゲートに加えて実行中の再判定も無効になります。

`status.json` は5分ごとの同期なので判定に遅れがあります。送信済みターンの途中では止めません。フックを外すと再判定は効きません。Claude 以外では、OpenCode（`integrations/opencode` のプラグイン）と Pi（`integrations/pi/` の拡張）が要求の直前に残量を見直します。そのほかの CLI は起動時の判定だけです。
