# quota-canvas-routing

Claude Code / Codex のセッション開始時に、quota-canvas の振り分け判定（AI ワーカーごとの利用枠 1 行と、振り分けのルール）を文脈に自動で入れます。セッションが AI ワーカーへ作業を振り分けるときの優先順の参考になります。

## しくみ

SessionStart フックで `quota-canvas routing hook` を 1 回実行し、その出力（1 行の JSON、`additionalContext`）をセッション開始時の文脈に渡します。判定は status.json を読むだけで、AI サービスへの取得はしません。何かあってもセッション開始を止めません（常に exit 0）。

## 前提

- macOS 専用です。
- quota-canvas をインストールして常駐（`quota-canvas run`）させ、`~/.config/quota-canvas/config.toml` で振り分け判定を有効にします:

  ```toml
  [routing]
  enabled = true
  # ワーカー名 → 窓 ID の対応を変えたいときだけ（省略時は provider ごとに 1 ワーカー）
  # [routing.workers]
  # glm = ["glm:5h"]
  ```

- フックは次の順で `quota-canvas` を探します: 環境変数 `QUOTA_CANVAS_BIN` → PATH 上の `quota-canvas` → `~/dev/quota-canvas/.venv/bin/quota-canvas`。
- status.json が無い・古い・壊れているときは、判定できない旨の 1 行だけを出します。何も出さないのは、`[routing] enabled = false`・判定できるワーカーが 0 件・`quota-canvas` が見つからないときです。

## 入れ方

Claude Code:

```
/plugin marketplace add digimaga/quota-canvas
/plugin install quota-canvas-routing@quota-canvas
```

Codex:

```
codex plugin marketplace add digimaga/quota-canvas
codex plugin add quota-canvas-routing@quota-canvas
```

Codex はフックを信頼（trust）するまで実行しません。信頼していないフックは、`codex exec` などでは確認も出ずに飛ばされるので、入れたあと一度、対話の Codex でこのフックを信頼してください（プラグインを更新してフックの中身が変わったときも同じ）。

## 無効化

`config.toml` の `[routing] enabled = false` にすると、フックは何も出さなくなります（プラグインを外す必要はありません）。

## 自分のワーカーラッパーから判定を使う

セッション開始以外（ワーカーを起動するラッパーなど）では:

```sh
quota-canvas routing check <ワーカー名>
```

使い切りのときだけ exit 3 になります。ラッパーを `quota-canvas routing check <名前> || exit 3` のようにしておくと、stop のワーカーを起こさずに済みます（`QUOTA_GATE_FORCE=1` で無視）。
