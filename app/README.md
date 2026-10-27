# QuotaCanvas.app（メニューバー常駐アプリ）

quota-canvas エンジン（Python CLI / LaunchAgent）のフロントエンド。SwiftUI + SwiftPM、
Command Line Tools だけでビルドできる。アプリは AI サービスや USB 画面と直接通信しない。
やり取りは `docs/app-contract.md` の CLI（`detect --json` / `config --json` /
`config --set-json` / `set-key`）と `~/Library/Application Support/quota-canvas/` の
`status.json`・`frame.png` の 2 ファイルのみ。

## ビルド

```sh
cd app && ./scripts/build.sh   # → build/QuotaCanvas.app（ad-hoc 署名、起動はしない）
```

Swift 6.2+ / swift-tools-version 6.0 / macOS 14 以上 / `swiftLanguageModes: [.v5]`。
WinKey（~/dev/winkey-mac）と同じ方式。

## 機能

- メニューバー（MenuBarExtra .window）: gauge 系 SF シンボル + 最も悪いカード状態の色の点
  （over/exhausted=赤, tight=黄, on_track=紫, spare/fresh=緑, unknown=灰。
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
- 設定（タブ 2 枚）:
  - サービス: `detect --json` の一覧。有効トグル（最大 5 サービス）、検出状態、プラン、
    窓チェックボックス（全チェック = `[]` として保存）、`needs_key` サービスの
    SecureField + 保存/削除（`set-key`、キーは stdin 経由で引数に載せない）。
    「再検出」ボタン。
  - 表示: ゲージ方式（remaining/used）、明るさ 0-100、夜間減光（開始/終了/明るさ）、
    通知を出す（F4）、エンジンのパス（UserDefaults、既定
    `~/dev/quota-canvas/.venv/bin/quota-canvas`）、ログイン時に起動（SMAppService.mainApp）。
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
- `Views/` — MenuPopoverView / SettingsView（表示タブ）/ ServiceSettingsView（サービスタブ）/ FramePreviewView
