# QuotaCanvas.app（メニューバー常駐アプリ）

quota-canvas エンジン（Python CLI / LaunchAgent）のフロントエンド。SwiftUI + SwiftPM、
配布ビルドには Command Line Tools が必要。アプリは AI サービスや USB 画面と直接通信しない。
やり取りは `docs/app-contract.md` の CLI（`detect --json` / `config --json` /
`config --set-json` / `set-key`）と `~/Library/Application Support/quota-canvas/` の
`status.json`・`frame.png` の 2 ファイルのみ。

## DMG からインストール

Apple Silicon、macOS 14 以上で [Releases](https://github.com/digimaga/quota-canvas/releases) の DMG を開き、QuotaCanvas.app を Applications にドラッグする。Homebrew と Python は不要。ad-hoc 署名のため初回は右クリックして「開く」、または「システム設定 > プライバシーとセキュリティ」から許可する。

設定の「表示」で同梱エンジンの常駐と TURZX 画面の使用を切り替える。リポジトリ版 LaunchAgent がある場合、同梱版は自動起動しない。

アンインストールは、設定で「エンジンを常駐させる」をオフにしてからアプリを削除する。オフにせず削除した場合は `~/Library/LaunchAgents/com.digimaga.quota-canvas.app*.plist` の各ラベルを `launchctl bootout` で停止してから plist を削除する。

## ビルド

```sh
cd app && ./scripts/build.sh && ./scripts/make-dmg.sh  # → build/QuotaCanvas-<version>.dmg
# ./scripts/build.sh --no-engine  # エンジン無しの開発用ビルド
```

Swift 6.2+ / swift-tools-version 6.0 / macOS 14 以上 / `swiftLanguageModes: [.v5]`。
SwiftPM で実行ファイルをビルドし、リソースと Info.plist を .app にまとめて ad-hoc 署名する。

## 機能

- メニューバー（MenuBarExtra .window）: gauge 系 SF シンボル + 最も悪いカード状態の色の点
  （over/exhausted=赤, tight/on_track=紫, spare/fresh=緑, unknown=灰。
  status.json が 5 分以上古いときは停止を表す灰色）。ポップオーバーには
  「今使う先」の推奨（`recommendation.text`、F1）、各カードの残り %（gauge=used なら使用量 %）・
  状態ラベル（`fresh` =「リセット直後」も緑系、F5）・リセットまで・advice・
  前回の期間の結果（`previous_used` →「前回 36% 余り」/「前回 64% 使用」、F3）・
  取得失敗中の理由（`stale_error` →「取得失敗中（最後の値）: …」、F7）、
  リセット権（`reset_credits`: 残り回数と最短の期限。24 時間未満は赤・2 日未満は黄）、
  更新時刻、errors。`status.json` は 10 秒ごとに再読込し、5 分以上古いと
  「エンジンが動いていません」を表示。一時停止/再開（`display.paused` を
  `config --set-json` で切替）、画面プレビュー、設定、終了。
- 通知（F4）: status.json を読み直すたびに前回との差分を見て、次のときだけ macOS 通知
  （UNUserNotificationCenter、権限は起動時に 1 回だけ要求。.app バンドル専用）:
  カードが over/exhausted に入った（同じ期間に同じ状態で 1 回）、resets_at が変わった
  （リセットされた。エンジン history の _same_reset と同じくパースした時刻を 60 秒の
  許容で比較し、通知キーの期間は 5 分単位に丸めるので微小なずれで繰り返し出ない）、
  リセット権が 24 時間以内（権利ごとに 1 回。前回との境界越え判定はせず、通知済みキーに
  無ければ出すので、権限が確定する前の読み込みで出せなかった権利も次の読み込みで出る）、
  `burst_exhausts_at` が新たに現れた（同じ期間で 1 回）。通知キーは
  UNUserNotificationCenter.add がエラー無しで受理した後に UserDefaults に保存し、
  再起動で再通知しない（トグルがオフのときは投稿せずキーだけ記録。
  権限が未確定・拒否のときは保存しない）。カードの遷移は初回読み込みでは差分の基準にするだけ。
  判定は View から切り離した純粋な型 `NotificationPlanner`（前回 status・今回 status・
  通知済みキーから通知リストを返す）。設定「表示」タブの「通知を出す」でオフにできる
  （UserDefaults、既定オン）。
  status.json の定期読み込みはアプリ起動時に始めるので、メニューを閉じている間も
  通知とメニューバーの点の変化が動く。
  ポップオーバーはエンジン停止（5 分以上更新なし）のときも直前のカードとリセット権を
  残して表示し、警告を上に出す。
  新フィールド（`recommendation` / `reset_credits` / `previous_used` / `stale_error` /
  `exhausts_at` / `burst_exhausts_at` / status `fresh`）はすべて Optional で受け、
  型が違ったり無かったりしても status 全体のデコードは壊れない（エンジンがまだ書いていない場合も表示は従来どおり）。
- 設定（タブ 4 枚）:
  - サービス: `detect --json` の一覧。有効トグル（最大 5 サービス）、検出状態、プラン、
    窓チェックボックス（全チェック = `[]` として保存）、`needs_key` サービスの
    SecureField + 保存/削除（`set-key`、キーは stdin 経由で引数に載せない）。
    「再検出」ボタン。
  - 表示: ゲージ方式（remaining/used）、明るさ 0-100、夜間減光（開始/終了/明るさ）、
    通知を出す（F4）、エンジンのパス（UserDefaults、既定
    同梱エンジン、無ければ `~/dev/quota-canvas/.venv/bin/quota-canvas`）、ログイン時に起動（SMAppService.mainApp）。
  - 振り分け: AI ワーカー振り分け判定（config.toml の `[routing]`、既定オフ）。
    「AI ワーカーの振り分け判定を使う」トグル（`routing.enabled`）、
    「止める下限（残り %）」Stepper（`routing.floor_percent` 0-50）と
    「古いとみなす（分）」Stepper（`routing.stale_minutes` 5-120）。
    「現在の判定」は status.json の `routing.workers` の一覧（判定ラベルは色付き:
    stop=赤 / reserve=黄 / go=紫 / boost=緑 / unknown=灰、残り %、理由の短い文）。
    表示中は 60 秒ごとに読み直し、判定時刻が 5 分より古いか不明なら警告する。
    行ごとの Picker で「標準」「使い切り」「監視のみ」を選ぶ。`routing mode --json`
    の一覧を真実値とし、変更後は `refresh` と 2 秒後の再読込で判定を更新する。
    使い切りの既定期限は次の 23:59。監視のみは取得を続け、振り分けでは stop にする。
    判定オフまたは判定なしでも全ワーカーのモード一覧と Picker を表示し、
    使い切り・監視のみには「解除」も表示する。
    「ワーカーの設定」は `routing.workers` のエディタ。追加ボタンで行を作り、名前は
    `^[A-Za-z0-9._-]{1,40}$` をアプリ側でも検証する。窓 id はカンマ区切り・末尾 `*`
    で前方一致し、上下ボタンで並べ替える。1 文字ごとの自動保存はせず「保存」ボタン
    （空行・不正な名前・重複・窓なしのときは無効と理由表示）と「元に戻す」。
    行を消すのはゴミ箱と「既定に戻す（サービスごと）」（確認ダイアログ付きで
    workers を空にする）だけ。保存は 1 本に直列化し、そのたびに `config --json`
    を取り直して変更した項目だけを反映して送る（他のタブの保存を壊さない）。
    JSON のオブジェクトはキー順を保てないため、 workers は
    `[{"name", "windows"}]` の配列で送り、読み込み時は `worker_order` の順。
    「Claude Code / Codex への組み込み」はプラグインのインストールコマンドの
    コピーボタン（設定ファイルは書き換えない旨の注記つき）。
  - 設定変更は `config --json` の現在値に反映して全体を `config --set-json` で保存。
    エラー JSON はアラート表示。
- 画面プレビュー: `frame.png` を幅に合わせて縮小表示、10 秒ごとに再読込。

## 構成

- `Models.swift` — Codable モデル（status.json / detect）+ 共有ヘルパ
- `ConfigDocument.swift` — 設定 JSON の可逆ラッパ（知らないキーを壊さず送り返す）
- `EngineClient.swift` — Process で CLI を呼ぶ（stdout を JSON、キーは stdin）
- `StatusStore.swift` — status.json の 10 秒ポーリング + ログイン時起動（SMAppService）
- `NotificationPlanner.swift` — F4 通知の差分判定（純粋な型。前回 status → 通知リスト）
- `NotificationCoordinator.swift` — 通知の投稿（UNUserNotificationCenter）+ 通知済みキーの保存（UserDefaults）
- `Views/` — MenuPopoverView / SettingsView（表示タブ）/ ServiceSettingsView（サービスタブ）/ RoutingSettingsView（振り分けタブ）/ FramePreviewView
