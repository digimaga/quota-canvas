# Menu-bar app ⇄ engine contract

The Python engine (`quota-canvas`, runs as a LaunchAgent) fetches usage and draws the
TURZX screen. The SwiftUI menu-bar app (`app/`, "QuotaCanvas.app") is only a front end:
it never talks to the AI services or the USB screen itself. Everything goes through the
CLI below and two files. All JSON is UTF-8; times are ISO 8601 with offset.

The app finds the CLI at the path stored in its settings (default
`~/dev/quota-canvas/.venv/bin/quota-canvas`).

## Files (in `~/Library/Application Support/quota-canvas/`)

- `status.json` — written atomically by `quota-canvas run` after every redraw:

```json
{
  "updated_at": "2026-10-24T21:30:00+09:00",
  "paused": false,
  "gauge": "remaining",
  "layout": [
    { "main": "claude:weekly", "subs": ["claude:5h"], "size": "large" },
    { "main": "codex:weekly", "subs": [], "size": "small" }
  ],
  "colors": { "claude": "#D97757", "codex": "#10A37F", "glm": "#3B82F6" },
  "cards": [
    {
      "id": "codex:weekly", "provider": "codex", "name": "Codex Pro", "label": "週間",
      "used": 80.0, "remaining": 20.0,
      "status": "over",                 // over|tight|on_track|spare|exhausted|fresh|unknown
      "status_label": "ペース超過",
      "advice": "ペースを約97%落とす · 残り6日は1日3%まで",
      "resets_at": "2026-10-31T02:40:44+09:00",
      "fetched_at": "2026-10-24T21:29:12+09:00",    // optional; when this data was fetched
      "exhausts_at": "2026-10-25T15:40:44+09:00",   // optional; OVER のとき
      "burst_exhausts_at": "2026-10-24T23:10:00+09:00",  // optional; 直近2時間の加速のとき
      "previous_used": 64.0,            // optional; 前回の期間の最終使用率
      "stale_error": "HTTP 429"         // optional; only while showing last good data
    }
  ],
  "errors": [ { "provider": "glm", "name": "GLM", "error": "HTTP 401" } ],
  "windows": [
    { "id": "codex:weekly", "provider": "codex", "key": "weekly", "part_of": null,
      "used": 80.0, "remaining": 20.0, "status": "over", "length": 604800.0,
      "resets_at": "2026-10-31T02:40:44+09:00",
      "exhausts_at": "2026-10-25T15:40:44+09:00",
      "fetched_at": "2026-10-24T21:29:12+09:00" }
  ],
  "routing": {
    "at": "2026-10-24T21:30:00+09:00",
    "reset_credits": { "codex": 1 },
    "holds": { "codex:weekly": "2026-10-31T02:40:44+09:00" },
    "workers": [
      { "name": "codex", "verdict": "boost", "reason": "burn", "windows": ["codex:weekly"],
        "remaining": 20.0, "status": "over",
        "resets_at": "2026-10-31T02:40:44+09:00",
        "exhausts_at": "2026-10-25T15:40:44+09:00",
        "burn_until": "2026-10-24T23:59:00+09:00", "stale": false, "provider": "codex" }
    ],
    "line": "quota: codex=boost(20%,ペース超過·~10/25 15:40枯渇見込み,リセット権1,使い切り~23:59,~10/31 02:40) (21:30時点)"
  },
  "reset_credits": {
    "codex": [
      { "title": "Full reset", "granted_at": "2026-09-30T03:45:33+09:00",
        "expires_at": "2026-10-30T03:45:33+09:00" }
    ]
  },
  "recommendation": {
    "provider": "codex", "name": "Codex", "window_id": "codex:weekly",
    "text": "Codex を使う · 週の残り 62% · リセットまで 1日3時間",
    "ranking": [
      { "provider": "codex", "name": "Codex", "window_id": "codex:weekly",
        "usable": true, "score": 1.23, "remaining": 62.0,
        "resets_at": "2026-10-26T02:40:44+09:00", "reason": "週の残り 62% · リセットまで 1日3時間" }
    ]
  }
}
```

- `stale_error` on a card — optional; present only while the engine keeps showing that
  provider's last successful data because current fetches fail (HTTP 429, network errors,
  …) — regardless of age. The value is the current failure reason; it disappears on the
  next successful fetch.

- `fetched_at` on a card — optional; when the card's data was actually fetched from the
  service. Unlike the top-level `updated_at` (bumped on every redraw), it does not move
  while the engine redraws without fetching: while the Mac sleeps (`sleep_with_mac`),
  during HTTP 429 backoff, and while showing last good data (then it is the time of the
  last successful fetch). Use it, not `updated_at`, to judge how old the numbers are.
  Omitted when unknown.

- `exhausts_at` on a card — optional; present only for status `over`: when the projected
  consumption reaches 100% (before the reset). `burst_exhausts_at` — optional; present
  only while the last-2-hour speed is clearly faster than the whole-window speed (≥1.5×
  and ≥1% moved in 2 hours): when 100% would be reached if that burst continued. Both are
  omitted when unknown.

- `previous_used` on a card — optional; the final used % of the window's previous period
  (recorded when the window resets). Omitted when the previous period is unknown.

- `status: "fresh"` — right after a reset with barely any usage yet (the old engine
  reported `unknown` here). Meaning: the window is open; the color matches `spare`.

- `recommendation` — optional top-level; present when at least one service is usable
  ("which service to use now", F1). `text` is the one-line advice shown in the header;
  `ranking` lists every non-error service, best first (`usable: false` entries are kept
  for context but are never recommended; their `reason` then says why — e.g. 「リセット待ち」
  when a window is past its reset time, 「取得失敗中」 while the engine is showing the
  provider's last good data). A service is not usable when any of its independent windows
  is over/exhausted, below 5% remaining, past its reset time, or the provider is in
  stale-error state. `score` = the share of the representative window (the one with the
  latest reset) predicted to expire unused, times the representative window's length in
  hours, divided by the hours until that reset (i.e. the speed at which "window-hours of
  value" would be lost — longer windows outrank short ones) — halved when a short window
  of the same service is below 20%, ×1.5 when a Codex reset credit expires within 24
  hours.

- `balances` — top-level array of `{provider, name, currency, amount, label, fetched_at, stale_error}`. Amounts are display-only and do not affect routing or pacing. `label` is the optional display prefix (for example, usage or remaining limit); when absent, display the localized "Balance" prefix; `stale_error` marks last-good data after a fetch failure, and `fetched_at` is the original successful fetch time. Empty when no balances are available.

- `reset_credits` — optional; present only when at least one service reports unused
  "rate-limit reset credits" (the right to instantly reset the weekly window, Codex).
  Map of provider → list of `{title, granted_at, expires_at}` (ISO 8601 with offset,
  `null` when unknown), earliest expiry first. Read-only: the engine never redeems a credit.

- `windows` — every window of every fetched provider, BEFORE the display filter
  (`display.windows` / `providers.<p>.windows` do not affect it; breakdown windows have a
  non-null `part_of`, the parent window's `key`, not its `id`). Written on every redraw so the
  routing CLI can judge all windows. Fields mirror the cards (`id`, `provider`, `key`,
  `part_of`, `used`, `remaining`, `status`, `resets_at`, `length` in seconds, plus the optional `exhausts_at`,
  `fetched_at`, `stale_error`). Written always, also while `routing.enabled = false` and
  while paused.

- `routing` — optional top-level; present only while `[routing] enabled = true` in the
  config. The routing verdict per worker (a named group of windows, `[routing.workers]`),
  computed by the same pure logic as `quota-canvas routing line`. `at` is the `updated_at`
  the judgment is based on, `reset_credits` maps provider → number of valid reset credits
  (display only, it never changes a verdict), `holds` maps window IDs to reset times for
  reserve/over windows and preserves the short-window hysteresis across redraws, and
  `line` is the same one-line summary
  `routing line` prints (for Macs without quota-canvas that only read a copied
  status.json). Per worker: `verdict` is one of
  - `stop` — do not use: exhausted (`reason: "exhausted"`) or below the floor
    (`routing.floor_percent`, `reason: "below_floor"`);
  - `reserve` — usable but pace is over (`reason: "over"`); prefer another worker;
  - `go` — normal use (`reason: "tight"` / `"on_track"` / `"over_near_reset"`);
    `over_near_reset` means a short window is projected to exhaust within its reset margin;
  - `boost` — plenty of headroom (`reason: "spare"` / `"fresh"`, or `"on_track"` for
    windows shorter than 24 hours), or lifted there by an
    active burn window (`reason: "burn"`);
  - `unknown` — cannot judge (`reason: "missing"` no matching window / `"error"` provider
    fetch error / `"stale"` stale `fetched_at` or `stale_error`), decided by the worst
    window of the worker. `stale: true` marks stale workers.
  OpenRouter, DeepSeek and Kimi are excluded from routing verdicts even when their windows are specified explicitly. With no `[routing.workers]` in the config, each other fetched provider becomes a single worker
  judged by its worst independent window — Gemini's worker then also includes the Argon
  (`gemini:argon`) and Antigravity-Claude (`gemini:claude`, `gemini:claude_5h`) windows;
  split it with an explicit `[routing.workers]` (e.g.
  `gemini = ["gemini:weekly", "gemini:5h"]` / `argon = ["gemini:argon"]`).
  Never blocks anything by itself — an `unknown` verdict must not stop the app or the
  session from starting (fail-open). The engine itself never acts on `routing`; it is
  information only. macOS only (the burn store lives under `~/Library/Application
  Support/`).

- `layout` — an array of `{main, subs, size}`, the panel placement actually used for the
  last frame, in panel order
  (`main` = window id or provider id, `subs` = sub window ids, `size` = `"large"|"small"`).
  Columns are assembled by reading the panels in order (`render.group_columns`): a `large`
  panel fills one column; a `small` panel joins the bottom row of the previous column when
  that column is a small column holding only a top row, otherwise it starts a new column
  (max 4 columns). A small entry with `main: ""` is an empty slot: it stays empty and keeps
  its position, so it is always reported when a small column has an empty top row and a
  filled bottom row (a trailing empty bottom slot may be reported too). An unresolvable
  `main` in a small slot renders empty the same way. A balance-only service can use its
  provider id as `main`: it renders a balance panel, with each amount prefixed by its
  `label` or the localized default "Balance" when `label` is null. With no `[[layout.panels]]` in the
  config this is the engine's auto layout (`auto_layout`),
  so the app can prefill its placement screen with what is on screen right now.
- `colors` — the effective theme color per provider (`#RRGGBB`): the engine defaults
  (claude `#D97757`, codex `#10A37F`, glm `#3B82F6`, …) overridden by `providers.<p>.color`.

- `frame.png` — the last 1920×462 frame sent to the screen. While paused nothing is sent
  and the file is left as is.

- `periods.jsonl` — engine-internal log of closed window periods (one line =
  `{"window_id", "resets_at", "used", "closed_at"}`), the source of the cards'
  `previous_used`. Written by the engine when a window's `resets_at` changes; the app
  never reads or writes it.

- `refresh-request` — empty marker file the app creates to ask for an immediate refresh
  (usually via `quota-canvas refresh`). The engine checks for it once a second, deletes it
  (a failed delete is only logged) and starts its next loop immediately, so a saved config
  or a refresh request shows up on screen within 1–2 seconds. Content is ignored. On a
  refresh request the engine fetches usage again — unless the last fetch was less than
  30 seconds ago (HTTP 429 protection), in which case it only redraws; a fetch moves the
  next periodic fetch to `poll.interval` after it. A config change alone never refetches.
  While the Mac (system or display) is asleep and `display.sleep_with_mac` is on, the
  engine never fetches usage, neither periodically nor on a refresh request; an overdue
  fetch happens right after wake.
  While paused the engine still consumes the request and rewrites `status.json`, but sends
  nothing to the screen.

## CLI

All commands print JSON on stdout with `--json`, exit 0 on success, and on failure exit
non-zero with `{"error": "<日本語の短い説明>"}` on stdout.

- `quota-canvas detect --json` — which services are usable on this Mac (makes the usage
  request once per detected service to learn the plan and windows; no AI tokens used):

```json
[
  { "provider": "claude", "name": "Claude", "detected": true,
    "source": "Claude Code のログイン（キーチェーン）", "needs_key": false,
    "plan": "Max", "windows": [ {"key": "5h", "label": "5時間"}, {"key": "weekly", "label": "週間"} ],
    "error": null },
  { "provider": "glm", "name": "GLM (Z.ai)", "detected": false,
    "source": null, "needs_key": true, "plan": null, "windows": [], "error": "API キーが未設定" }
]
```

- `quota-canvas config --json` — effective config (defaults filled in), same shape as the
  TOML: `{"display": {...}, "layout": {"panels": [...]}, "poll": {...}, "pacing": {...}, "providers": {"claude": {"enabled": true, "windows": []}, ...}}`.
  `pacing` is always `{"tight", "on_track"}` plus an optional `sleep` object
  (`{"start", "end", "skip", "advice"}`, absent when unset) — like `display.night`, unset
  sections are absent, so the app's copy-and-send-back round trip preserves keys it does
  not edit. `routing` additionally carries `worker_order` (array of worker names): JSON
  objects have no key order (the app's JSONSerialization reshuffles them), so the
  `[routing.workers]` order — which is the `routing line` display order — travels there too.
- `quota-canvas config --set-json` — reads a full config object (same shape) on stdin,
  validates it, writes `~/.config/quota-canvas/config.toml`. The running engine watches the
  file's mtime once a second and picks it up within 1–2 seconds (redraw only, no refetch).
  Prints the effective config back. `routing.workers` may also arrive as an array of
  `{"name": …, "windows": […]}` objects (the app's form: an array keeps the order that
  JSON objects cannot), and dict-form `workers` are reordered by `worker_order` when
  present; the TOML is written in that order.
- `quota-canvas refresh [--json]` — creates `refresh-request` (above) and exits; it does
  not check whether the engine is running. With `--json` it prints `{"requested": true}`.
- `quota-canvas pick [--json]` — which service to use right now, as one line (the same
  text as status.json's `recommendation.text`). Reads status.json; when it is missing or
  older than 20 minutes, the CLI fetches usage once and computes the recommendation
  itself (it never sends to the screen and never writes status.json). With `--json` it
  prints the recommendation object as-is, or `{"recommendation": null}` when nothing is
  usable.
- `quota-canvas set-key <provider>` — reads an API key from stdin (no echo, no argv, so it
  never shows in `ps`) and stores it in the login Keychain, service `quota-canvas`, account
  `<provider>`. `quota-canvas set-key <provider> --delete` removes it. Only for services
  with `needs_key: true`. With `--json` it prints `{"provider": "<p>", "stored": true}`
  (or `"deleted": true`).

- `quota-canvas routing line [--json]` — the routing verdict as one line, e.g.
  `quota: glm=go(89%,~23:07) … (21:30時点)` (same format as `~/.claude/bin/quota-gate
  line`). Reads status.json only — it never fetches from the services — and always exits
  0, even when status.json is stale (prints a `quota: stale（…）` line) or the judgment
  fails (one stderr line). With `--json`, a successful judgment is
  `{"at": ..., "reset_credits": {...}, "holds": {...}, "workers": [...]}` (no `line`);
  stale status returns `{"state": "stale", "detail": "missing|old|broken|unreadable"}`.
  Silent and exits 0 when `routing.enabled = false`.
- `quota-canvas routing check <name> [--json]` — for a SessionStart hook (10 s budget):
  `stop` → one stderr line, exit 3; `reserve` → one stderr note, exit 0; `unknown` → one
  stderr line (「判定できないためそのまま続行」), exit 0; `go`/`boost` → silent, exit 0.
  A name is valid when it is one of `KNOWN_PROVIDERS` (claude / codex / glm / grok /
  gemini) or a `[routing.workers]` key — anything else exits 2, regardless of what
  status.json contains. A valid name whose windows are absent from status.json (or when
  status.json is missing / too old / unreadable) is `unknown` with exit 0; with `--json`
  the reason distinguishes `missing` (no windows) from `missing_status` / `stale` /
  `broken` (the status.json itself). Silent exit 0 when `routing.enabled = false`, and
  `QUOTA_GATE_FORCE=1` forces silent exit 0 (skips the gate). `--status PATH` /
  `QUOTA_STATUS_PATH` and `--fallback-status PATH` / `QUOTA_STATUS_FALLBACK` choose the
  status.json (the fallback is used when the primary is missing or older than
  `routing.stale_minutes`).
- `quota-canvas routing burn [name on|off [--until HH:MM|ISO]]` — burn mode: while active
  (until the time, default the next 23:59), that worker's `reserve`/`go`/`boost` counts as
  `boost` (display and judgment; `stop`/`unknown` stay). `stop`/`unknown` are never
  lifted. State lives in `~/Library/Application Support/quota-canvas/routing-burn.json`;
  expired entries are purged on write. `quota-canvas routing burn` lists active entries.
  With `--json`, both the list and `on|off` print the active entries after the operation
  as `{"burn": [{"name": "codex", "until": "2026-10-24T23:59:00+09:00"}, …]}` (name
  order); on failure it exits 2 with `{"error": …}` on stdout. Without `--json` the
  output is unchanged.
- `quota-canvas routing hook [--event NAME]` — one line of JSON for the SessionStart hook
  of the bundled plugin (`plugins/quota-canvas-routing/`):
  `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "…"}}`
  (`--event` renames `hookEventName`, default `SessionStart`). `additionalContext` is the
  same one-line verdict `routing line` prints plus short routing rules in the display
  language; when status.json is missing/too old/unreadable it is only the same
  `quota: stale（…）` line. Same read path and options as `routing line` (`--status`,
  `--fallback-status`, `QUOTA_STATUS_PATH`, `QUOTA_STATUS_FALLBACK`); never fetches from
  the services. It always exits 0 and prints nothing on stdout when
  `routing.enabled = false`, when there is no worker to judge, when the config cannot be
  read, and when the judgment throws (fail-open, so a broken engine never blocks the
  session from starting). Worker names shown in the line are restricted to
  `^[A-Za-z0-9._-]{1,40}$` (default workers take their names from status.json's
  `windows[].provider`, so anything else is dropped there and in the hook).


Notes on `detect --json`: `detected: true` means the credentials exist. It then makes the
usage request once; if that request fails, `plan`/`windows` stay empty and `error` carries
the short reason (e.g. `"HTTP 429"`). The `key` values in `windows[]` are exactly what the
app may put into `providers.<p>.windows` (empty list = show every window).

## Config keys the app edits

| key | type | UI |
|---|---|---|
| `display.paused` | bool | menu: 一時停止 / 再開 (engine stops sending frames, frees the screen) |
| `display.gauge` | `"remaining"` \| `"used"` | 表示: 残量で表示（Codex 方式）/ 使用量で表示（Claude 方式） |
| `display.percent_colors` | bool（既定 `true`） | 表示: 利用状況に合わせて % の文字色を変える（オフで全部白） |
| `display.separator` | string（5 文字まで、既定 `""`） | 表示: 区切り文字 picker。`""` = 表示言語の既定（日本語「／」、英語「 · 」）。TURZX 画面の文言の ` · ` を置き換える（status.json の文言は ` · ` のまま） |
| `display.language` | `"auto"` \| `"ja"` \| `"en"`, default `"auto"` | 表示: language picker. `"auto"` follows the macOS language setting |
| `display.brightness` | 0–100 | 表示: slider |
| `display.night` | `{start, end, brightness}` or absent | 表示: 夜間は暗くする toggle + times + slider |
| `display.sleep_with_mac` | bool, default `true` | — (engine only; UI なし). Mac がスリープしたら黒 1 枚 + 明るさ 0 で画面を消灯し、復帰したら明るさと表示を戻す |
| `layout.panels` | array of `{main, subs, size}`, `[]` = auto placement | 表示: 配置エディタ (placement). `main` = window id (`"claude:weekly"`) or provider id (`"gemini"`, engine picks the service's best window / shows a balance-only or error slot) or `""` (small のみ可: その段は空き枠になり位置を保つ); `subs` = sub window ids (large panels only, ≤ 3). キー省略または `null` = 自動（主枠と同じサービスの残りの窓、最大 3、ほかのパネルで使った窓は除く。small パネルはサブなし）、`[]` = 明示的にサブなし。status.json の `layout[].subs` は解決後の配列; `size` = `"large"` \| `"small"`. Columns are assembled in panel order — 1 `large` or 2 consecutive `small`s per column, at most 4 columns (all-small = up to 8 panels); the engine rejects more with `config --set-json`. status.json の `layout[]` は同じ順序なので、同じ規則 (`render.group_columns`) で列構成を復元できる |
| `providers.<p>.enabled` | bool | サービス: toggle per detected service |
| `providers.claude.token_refresh` | `"cli"` \| `"off"`, default `"cli"` | Claude の OAuth トークンの自動更新。`"cli"` = 期限切れ間近 / HTTP 401/403 で `claude -p` を軽く 1 回バックグラウンド実行し、Claude Code CLI 自身にキーチェーンへ書き戻させる（30 分に 1 回まで。成功したら反映要求を出して 1 秒以内に取り直す）。`"off"` = 自動更新しない（401/403 は「トークン期限切れ（claude を一度起動してください）」のエラー表示） |
| `providers.<p>.windows` | list of window keys, `[]` = all | サービス: checkbox per window the service reports |
| `providers.<p>.color` | `"#RRGGBB"` or absent | サービス: theme color picker (bar fill + name dot). Absent = engine default brand color |
| `routing.enabled` | bool（既定 `false`） | 振り分け: 「AI ワーカーの振り分け判定を使う」トグル。true で `routing line|check|hook` が判定を出し、status.json に `"routing"` が現れる |
| `routing.floor_percent` | 0–100（既定 5） | 振り分け: 「止める下限（残り %）」Stepper。floor 適用中に残りがこれ未満のワーカーは stop |
| `routing.floor_hours` | number ≥ 0（既定 24） | 長い窓の floor を適用する残り時間の下限（時間）。0 なら常に適用 |
| `routing.short_over_margin_minutes` | number > 0 or `null`（既定 `null`） | 短い窓の終盤の猶予（分）。`null` は窓の長さの 10% |
| `routing.stale_minutes` | 1 以上（既定 20） | 振り分け: 「古いとみなす（分）」Stepper。fetched_at がこれより古い窓を含むワーカーは stale |
| `routing.workers` | ワーカー名 → 窓 id 配列（`config --set-json` は `[{"name", "windows"}]` の配列も可、`worker_order` でも順指定可）、空 = サービスごとの既定 | 振り分け: ワーカーエディタ（名前 + 窓 id のカンマ区切り、上下ボタンで並べ替え、「既定に戻す（サービスごと）」で空にする）。名前は `^[A-Za-z0-9._-]{1,40}$` |

Strings the engine returns or draws — `advice`, `status_label`, `label`, `error`
(in status.json and CLI `{"error"}`), panel text — are in the language chosen by
`display.language` (`"auto"` = follow the Mac's language, Japanese or English).

## Desktop overlay frames

The app writes `desktop-request.json` atomically beside `frame.png` with
`{"pixel_width": N}` for the overlay's actual width in display pixels, and removes
it when the overlay is disabled. On each frame write the engine clamps the requested
width to 3840; above 1920 it atomically writes a matching high-resolution
`desktop.png`, otherwise it removes that file. Invalid or missing requests remove
`desktop.png`. A request older than 24 hours is invalid; while enabled the app
touches its request approximately hourly. The app uses `desktop.png` when it is
no more than 15 seconds older than `frame.png`, and otherwise displays
`frame.png`. Paused or sleeping frames remain unchanged until the next normal redraw.
