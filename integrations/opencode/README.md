# Orca 用 OpenCode 残量ガード

OpenCode 2.3.2 の `model.request` hook で、選択中の provider に対応する worker の残量を要求ごとに確認します。Orca の pane 内で `ORCA_PANE_KEY` が空でなく、`ORCA_GATE_HOOK` が `off` でないときにだけ動きます。モデルの切り替え後も各要求の provider を使います。

`OPENCODE_QUOTA_WORKERS` が設定されていれば `opencode-go=opencode,opendesign=opendesign` のような対応表として優先します。空文字は空の対応表です。未設定なら `~/.config/orca-agent-gate/config.json` の `opencode_providers` を読みます。どちらにも指定がなければ対応表は空です。

```json
{"opencode_providers": {"opencode-go": "opencode", "opendesign": "opendesign"}}
```

各要求で `~/.local/bin/orca-agent-gate verdict <worker>` を呼びます。stop は要求を中断し、一時許可された stop、reserve、unknown は警告して続行します。CLI 失敗、不正な応答、5 秒のタイムアウトは unknown です。通知は worker の状態が変わったときに出します。同じ worker の同時照会は共有しますが、完了結果はキャッシュしません。`model.request` はタイトル生成などの要求にも適用されます。

worker は英数字、`.`、`_`、`-` の 1〜40 文字で、先頭の `-` と `off`、`.`、`..` は使えません。不正な worker は照会せず unknown として通知します。設定を読めない場合や JSON が壊れている場合は通知して対応表なしで続行し、通常ファイル以外は読みません。設定の読み込みにも 5 秒の期限があります。verdict の子プロセスには `OPENDESIGN_API_KEY` を渡しません。

`./integrations/opencode/install.sh --dry-run` で確認し、`./integrations/opencode/install.sh` で配置後、OpenCode を再起動してください。同じ内容が配置済みなら変更しません。異なる既存ファイルは `quota-guard.js.bak` に退避し、退避先が既に存在する場合は dry-run でも停止します。

stop の場合は回復を待って再送するか、必要に応じて `orca-agent-gate allow <worker>` で一時許可します。警告はプラグインの標準エラーに出力します。表示位置は OpenCode の起動形態に依存します。
