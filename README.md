# quota-canvas

Mac から TURZX 9.2インチ USB セカンダリースクリーン（1920×462 IPS）に画像を表示するツール。

この画面は macOS のディスプレイとしては認識されず、USB 機器「TURZX1.0」（VID `0x1cbe` / PID `0x0092`）として見える。通信は [phstudy/turing-smart-screen-cli](https://github.com/phstudy/turing-smart-screen-cli)（Turing Smart Screen 8.8" V1.1 用、MIT）と同じプロトコルで、そのライブラリを固定コミットで依存に入れて使っている。

## セットアップ

```bash
./scripts/setup.sh
```

Homebrew の `libusb` を入れ、`.venv` にこのパッケージをインストールする。sudo は不要。

## 使い方

```bash
.venv/bin/turzx test                 # 向き確認用のテストパターン
.venv/bin/turzx show picture.png     # 横長画像を表示（1920×462 に合わせて拡大・切り抜き）
.venv/bin/turzx brightness 80        # 明るさ 0〜100
.venv/bin/turzx sync                 # 接続確認（応答に "turzx_0001_0015" が含まれる）
```

Python から使う場合:

```python
from turzx import Screen
Screen().show(pil_image)   # 1920×462 の横長画像
```

## 画面の仕様メモ

- 画像は PNG で送る。デバイスが受け取るのは **縦向き 462×1920** のフレーム。`show` は横長 1920×462 の画像を `--rotation`（既定 90 度）だけ回転させて送る。
- 1920×462 や 480×1920 をそのまま送ると表示が崩れる。
- 縦向きの画像を回転なしで送るときは `show-raw` を使う。

## QuotaCanvas（AI 利用枠のペース表示）

Claude / Codex / GLM / Grok / Gemini の利用枠を取得し、「今のペースで使い続けるとリセット前に尽きるか」を画面に出す。参考: [nuitsjp/token-dashboard](https://github.com/nuitsjp/token-dashboard)（Windows 版）。

```bash
.venv/bin/quota-canvas once                     # 1 回取得して表示
.venv/bin/quota-canvas run                      # 15 分ごとに取得・1 分ごとに再描画（Mac のスリープ中は取得しない）
.venv/bin/quota-canvas --no-screen --out a.png once   # 画面に送らず PNG だけ
.venv/bin/quota-canvas --only claude:weekly codex:weekly once   # 表示する枠を絞る
.venv/bin/quota-canvas config                   # 実際に使われる設定と窓 ID の一覧を表示
.venv/bin/quota-canvas config --init            # 設定ファイルをひな形から作成
.venv/bin/quota-canvas detect --json            # 使えるサービスとプラン/窓を表示
.venv/bin/quota-canvas set-key glm              # GLM の API キーをキーチェーンに保存
.venv/bin/quota-canvas pick [--json]            # 今どのサービスを使うのが得かを 1 行（または JSON）で
./scripts/install-agent.sh                      # ログイン時に自動起動（--uninstall で解除）
./scripts/install-auto-update.sh                # 10 分ごとに main の新しい commit を取り込み、常駐を再起動（--uninstall で解除）
```

- 設定ファイル: `~/.config/quota-canvas/config.toml`（ひな形はリポジトリ直下 `config.example.toml`）。表示する窓の選択と順序（`display.windows`）、明るさと夜間減光（`display.brightness` / `display.night`）、一時停止（`display.paused`）、Mac スリープに合わせた画面の消灯と復帰（`display.sleep_with_mac`、既定 true）、ゲージの向き（`display.gauge`: `"remaining"` 残量 / `"used"` 使用量）、取得・再描画間隔（`poll`）、ペース判定の閾値（`pacing`、`pacing.sleep` で睡眠帯を除いた「起きている時間」での判定も可。`pacing.sleep.advice = "bedtime"` ならアドバイス行の残り時間を「寝るまで」で出す）、プロバイダごとの有効/無効と表示する窓（`providers.*.enabled` / `providers.*.windows`、空 `[]` なら全部）を 1 ファイルで管理できる。ファイルが無ければすべて既定値。`run` 中に更新すると自動で読み直す。
- コマンドラインの `--only` / `--interval` / `--redraw` は設定ファイルより優先される。`--config PATH` で設定ファイルの場所を変えられる。
- メニューバーアプリ連携（`docs/app-contract.md`）: `run` は再描画のたびに `~/Library/Application Support/quota-canvas/` へ `status.json` と `frame.png` を原子的に書く。`display.paused = true` の間は画面に何も送らず、`status.json` だけ更新する。app は `detect --json` / `config --json` / `config --set-json` / `set-key` / `refresh` でエンジンを操作する。
- 「今すぐ反映」: `quota-canvas refresh` が反映要求ファイル `~/Library/Application Support/quota-canvas/refresh-request` を作ると、`run` が 1 秒以内に検知して再描画する（利用量の取り直しは前回の取得から 30 秒以上あいたときだけ）。設定ファイルの保存も 1〜2 秒で画面に反映される。
- 認証情報: Claude はキーチェーン「Claude Code-credentials」、Codex は `~/.codex/auth.json`、GLM は キーチェーン `quota-canvas/glm` → `ZAI_API_KEY` → `~/.config/glm-run/zai_api_key` → `~/.dsh/.credentials.yaml`、Grok は `~/.grok/auth.json`。トークンの更新は各 CLI に任せ、ここでは読むだけ（`set-key glm` で保存したキーが最優先で使われる）。ただし Claude は既定（`providers.claude.token_refresh = "cli"`）で、トークンの期限切れ間近か HTTP 401/403 のとき `claude -p` を軽く 1 回バックグラウンド実行して CLI 自身に更新させる（30 分に 1 回まで）。`"off"` にすると自動更新せず、401/403 は「claude を一度起動してください」のエラーになる。Grok（`providers.grok.token_refresh`）は期限切れのとき枠を使わない `grok models` を、Codex（`providers.codex.token_refresh`）は 401 のとき `codex exec … ok` を同じように 1 回実行する（Codex は枠を少し使うので 30 分に 1 回まで）。Gemini は Antigravity CLI `agy` の `/usage`（`agy -p "/usage" --output-format json`。モデルを呼ばないので枠を消費しない。トークンの更新も agy 自身が行う）から取得する。`providers.gemini.token_refresh` は過去の設定ファイルとの互換で受け付けるだけで、現在は使われない。窓は 週間（`gemini:weekly`）、Gemini 4 Argon の週間（`gemini:argon`）、5 時間（`gemini:5h`）、Antigravity 経由の Claude / GPT の週間（`gemini:claude`）とその 5 時間（`gemini:claude_5h`）の 5 つ。プラン名（例「Google AI Pro」）はキーチェーン service `gemini` / account `antigravity` のトークンで取得できれば出す（取れなければ「Gemini」）。
- 履歴: `~/Library/Application Support/quota-canvas/history.jsonl`（8 日分）。日別の棒グラフと直近 24 時間の速度に使う。履歴が始まる前の日は「?」。
- 判定: 窓全体の平均速度と直近速度を半々に混ぜて予測。リセット前に 100% に届く→ペース超過、リセット時 92% 以上→使い切り、70% 以上→順調、それ未満→余裕あり。リセット直後（経過 3% 未満かつ使用 5% 未満）は「リセット直後」。直近 2 時間の速度が混ぜた速度の 1.5 倍以上で、そのままだとリセット前に尽きるときは、アドバイスが黄色で「このペースだと HH:MM に尽きる」になる。同じサービスの 5 時間枠などが先に尽きるときもアドバイスで触れる。
- 前回の結果: 窓がリセットされるたびに、その期間の最終使用率を `periods.jsonl`（窓ごとに最新 20 件）に残し、次の期間のカードに「前回 36% 余り」と出す。
- 今使う先: 画面のヘッダと `pick` に「Grok を使う · 週間の残り 69% · リセットまで 19時間」のように出す。点数はリセット時に使われずに消える見込み量 × 窓の長さ ÷ リセットまでの時間。ペース超過・残り 5% 未満・取得失敗中・リセット待ちのサービスは選ばない。期限 24 時間以内の Codex リセット権があれば優先する。
- アドバイス文言は `src/quotacanvas/render.py` の `advice_text()` だけで決まる。
- AI ワーカー振り分け（任意、既定 OFF）: `[routing] enabled = true` にすると、窓ごとの残量・ペースからワーカー（窓のグループ）ごとに `stop / reserve / go / boost / unknown` を判定する。`.venv/bin/quota-canvas routing line` で 1 行表示（`~/.claude/bin/quota-gate` と同じ書式）、`routing check <名前>` は SessionStart フック用（stop で終了コード 3、`QUOTA_GATE_FORCE=1` で無効化、status.json を読むだけでサービスへは取得しない）、`routing burn <名前> on` で期限内はそのワーカーを boost 扱いにする。判定は fail-open（unknown でも起動は止めない）。詳細は `config.example.toml` の `[routing]` 節と `docs/app-contract.md`。

## ライセンス

MIT（[LICENSE](LICENSE)）。
