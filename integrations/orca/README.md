# Orca Agent Gate

Orca がエージェントの CLI を起動する前に、残量と任意の同時起動数を確認するラッパーです。Python 3.9 以降と標準ライブラリを使います。入力・出力・TTY を引き継ぎ、本物の CLI に引数をそのまま渡します。Devin のモデル指定だけは下記の規則を適用します。

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

判定コマンドは `ORCA_GATE_CHECK_CMD`、PATH または `~/.claude/bin` の `quota-gate check`、PATH の `quota-canvas routing check` の順に選び、末尾に worker を渡します。`ORCA_GATE_CHECK_CMD` はコマンドと固定引数を空白区切りで指定します（引用符を使えます）。exit 3 は停止、exit 0 の出力は注意として表示します。コマンドがない、10 秒で終了しない、その他の異常時は判定不能を通知して通常どおり起動します。タイムアウト時は判定コマンドのプロセスグループを終了します。判定コマンドの出力は一時ファイルで受けるため、出力が多くても判定は止まりません。CLI がラッパーへ `exec` で転送して同じ PID で再入した場合は停止します。転送スクリプトが子プロセスとしてラッパーを呼ぶ場合は検出できないので、`cli` には実体の実行ファイルの絶対パスを指定してください。

cap を設定すると `~/.local/state/orca-agent-gate/<worker>/<pid>` に起動時刻を保存します。次回の起動時に終了済み PID や再利用された PID を掃除します。同時本数の確認と PID 登録は worker ごとのロックで直列化します。任意の `board_keys` に文字列の配列を指定すると、`~/.claude/run/worker-board.json` の `total` 内の該当キーを合算します。`updated_at` が現在から 600 秒以内の JSON のみ有効です。ファイルがない、壊れている、古い場合は足しません。`ORCA_GATE_BOARD` で掲示板のパスを変更できます。掲示板と PID 記録が同じ起動を表す場合は重複して数えます。

Devin は `--model` がないとき `devin_model`（既定 `swe-2-high`）を追加します。指定済みモデルは `devin_allowed_models`（上記の6種類が既定）で照合し、許可外なら停止します。`acp` サブコマンドの場合は追加する `--model` を `acp` の直後に置きます。値のない `--model` は強制起動時も拒否します。`--model=value` 形式にも対応し、`--` 以降はプロンプトとして保持します。`off` はモデルの検査を無効化しません。

## 一時的な回避と限界

一時的に回避するには起動時に `ORCA_GATE_FORCE=1` を指定します。残量・cap・Devin のモデル拒否を飛ばし、環境変数は CLI に引き継ぎません。強制起動したプロセスは同時本数に含まれません。恒常的に残量・cap 判定を無効化するには対象 agent に `"off": true` を設定します。

起動ごとに `~/.local/state/orca-agent-gate/launch.log` に時刻・agent・worker・判定・決定を追記します。引数やプロンプトは保存しません。ログは自動ローテーションしません。

これは Orca が設定された Command を起動するときだけ有効です。Orca のチャット型の一部や保存セッション復元が Command を通らない場合、また Devin の画面内でのモデル切替は止められません。直接 CLI を実行すれば回避できます。PID の起動時刻は `ps -o lstart=` に依存し、同じ秒に PID が再利用された場合は区別できません。
