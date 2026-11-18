# Pi の DeepSeek 予約ゲート

Pi の通常のモデル要求では `before_provider_request`、コンパクションでは `session_before_compact` で、選択中のプロバイダを確認して ds-gate のリースを取得します。待機中はステータス行を 5 秒ごとに更新し、Esc による中断を取得通信に伝えます。対象外のプロバイダへの次の要求時、または `agent_settled`・手動コンパクション完了時・`session_shutdown` で解放します。同時の取得要求は共有します。

heartbeat は TTL の 3 分の 1 ごと（最大 30 秒）に送り、通信は最大 5 秒か残り TTL まで待ちます。取得した直後にも最初の送信の前に heartbeat を 1 回送り、FIFO 待ちや応答の遅れでリースが失効していないことを確かめてから期限を決めます。404 または TTL 内に更新を確認できない場合は実行を中断し、次の送信時に（更新結果が不明なリースは先に解放してから）再取得します。解放通信は最大 5 秒待ちます。

```sh
./integrations/pi/install.sh --dry-run
./integrations/pi/install.sh
```

インストール先は `~/.pi/agent/extensions/ds-gate-lease.ts` です。ゲート URL はリポジトリに含めないので、Mac ごとに `~/.config/deepseek-run/config.json` へ `{"gate_url": "http://<ゲートのアドレス>:<ポート>"}` を書くか環境変数で渡してください。既存ファイルは `.bak`（重複時は連番）に退避します。Pi を再起動または `/reload` してください。

| 環境変数 | 意味 |
| --- | --- |
| `PI_DS_GATE_PROVIDERS` | 対象プロバイダ名。カンマ区切り、既定 `gx10` |
| `PI_DS_GATE_URL` | ゲート URL。未指定時は `DEEPSEEK_RUN_GATE_URL`、次に `~/.config/deepseek-run/config.json` の `gate_url` |
| `PI_DS_GATE_KEY` | Bearer 鍵。未指定時は `DEEPSEEK_RUN_API_KEY`、`~/.config/deepseek-run/api_key` の 1 行目、`~/.dsh/.credentials.yaml`・`~/.dsh-resident/.credentials.yaml` の `GX10_DEEPSEEK_API_KEY` の順 |
| `PI_DS_GATE_WAIT` | サーバー側の最大待ち秒数。既定 600、0 は待たずに取得 |
| `PI_DS_GATE_DISABLE=1` | ゲートを完全に無効化する抜け道 |
| `PI_DS_GATE_FAIL_OPEN=1` | 取得・heartbeat 失敗時に警告して続行する抜け道 |

URL・鍵の未設定や取得失敗は既定で `ctx.abort()` により中断します。`PI_DS_GATE_FAIL_OPEN=1` では警告して続行します。鍵の値やサーバー応答の本文は表示しません。実際の待ち上限はサーバーの設定にも従います。

待機中に接続が切れた場合は、ゲートが待ち行列から要求を除外し、応答前に切断した要求にはリースを発行しない仕様を前提とします。通常終了やセッション切替、ユーザー中断では解放を試みますが、ネットワーク障害や強制終了では TTL による回収を待ちます。ステータス行は Pi の UI がある場合に表示します。

現在の Pi 拡張 API は `before_provider_request` に実際のモデルを渡さず、コンパクション内部の要求もそのイベントを通りません。この拡張は選択中の `ctx.model` で判定するため、virtual model などが別プロバイダへルーティングする場合は保護できません。自動・手動コンパクションは `session_before_compact` とその中断シグナルで保護しますが、ルーティング後のモデルを参照できない制約は同じです。実際に送るモデルを確実に保護するには Pi 側の API 対応が必要です。
