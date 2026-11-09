"""detect-cache.json: キャッシュの書き込み、`detect --json --cached` の即返しと
フォールバック、常駐側の更新 (cli._update_detect_cache) のテスト。

実際の通信・キーチェーンは使わない (detect_all / REGISTRY はモック)。
キャッシュのパスは tmp_path に差し替える。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from quotacanvas import cli, detect_cache, providers
from quotacanvas.models import ProviderUsage, Window
from quotacanvas.providers._base import Detection, Provider

NOW = datetime(2026, 10, 25, 9, 0, 0, tzinfo=timezone.utc)


def _run(monkeypatch, capsys, argv):
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def _entry(pid, **extra):
    base = {"provider": pid, "name": pid.title(), "detected": True, "source": "s",
            "needs_key": True, "plan": None, "windows": [], "error": None}
    base.update(extra)
    return base


def _patch_path(monkeypatch, tmp_path):
    path = tmp_path / "detect-cache.json"
    monkeypatch.setattr(detect_cache, "DETECT_CACHE_PATH", path)
    return path


# ------------------------------------------------------------- detect 側

def test_detect_writes_cache_every_run(monkeypatch, capsys, tmp_path):
    path = _patch_path(monkeypatch, tmp_path)
    services = [_entry("claude", plan="Max",
                       windows=[{"key": "weekly", "label": "週間"}])]
    monkeypatch.setattr(providers, "detect_all", lambda now=None: services)
    rc, out = _run(monkeypatch, capsys, ["detect", "--json"])
    assert rc == 0
    assert json.loads(out) == services                 # 従来どおり配列
    data = json.loads(path.read_text(encoding="utf-8"))
    written = [{k: v for k, v in e.items() if k != "fetched_from"}
               for e in data["services"]]
    assert written == services
    assert all(e.get("fetched_from") for e in data["services"])  # provider 単位の時刻
    assert data["cached_at"]                           # ISO 時刻が入る
    assert data["fetched_from"]                        # 取得の開始時刻も入る


def test_detect_cached_returns_cache_without_fetch(monkeypatch, capsys, tmp_path):
    path = _patch_path(monkeypatch, tmp_path)
    payload = {"cached_at": "2026-10-25T18:00:00+09:00",
               "services": [_entry("glm", detected=False, source=None,
                                   error="API キーが未設定")]}
    path.write_text(json.dumps(payload), encoding="utf-8")

    def detect_all(now=None):
        raise AssertionError("--cached では全取得しない")

    monkeypatch.setattr(providers, "detect_all", detect_all)
    rc, out = _run(monkeypatch, capsys, ["detect", "--json", "--cached"])
    assert rc == 0
    assert json.loads(out) == payload                  # {"cached_at", "services"} 形


def test_detect_cached_without_cache_falls_back_and_writes(monkeypatch, capsys, tmp_path):
    path = _patch_path(monkeypatch, tmp_path)
    services = [_entry("claude")]
    calls = []

    def detect_all(now=None):
        calls.append(now)
        return services

    monkeypatch.setattr(providers, "detect_all", detect_all)
    rc, out = _run(monkeypatch, capsys, ["detect", "--json", "--cached"])
    assert rc == 0
    assert len(calls) == 1                             # 1 回だけ全取得
    data = json.loads(out)
    assert data["services"] == services and data["cached_at"]
    written = [{k: v for k, v in e.items() if k != "fetched_from"}
               for e in json.loads(path.read_text(encoding="utf-8"))["services"]]
    assert written == services


def test_detect_cached_broken_cache_falls_back(monkeypatch, capsys, tmp_path):
    path = _patch_path(monkeypatch, tmp_path)
    path.write_text("{oops", encoding="utf-8")
    services = [_entry("claude")]
    monkeypatch.setattr(providers, "detect_all", lambda now=None: services)
    rc, out = _run(monkeypatch, capsys, ["detect", "--json", "--cached"])
    assert rc == 0
    assert json.loads(out)["services"] == services


def test_read_cache_rejects_structurally_invalid(monkeypatch, tmp_path):
    """形は JSON でも必須項目・型が合わないキャッシュは無効扱いする
    (そのまま返すとアプリ側のデコードが失敗し続ける)。"""
    path = _patch_path(monkeypatch, tmp_path)
    iso = "2026-10-25T18:00:00+09:00"
    invalid = [
        {"cached_at": iso, "services": [{}]},                          # 必須項目なし
        {"cached_at": iso, "services": [{"provider": "x", "detected": 1,
                                         "needs_key": True,
                                         "windows": []}]},             # detected が bool でない
        {"cached_at": iso, "services": [{"provider": "x", "detected": True,
                                         "needs_key": True,
                                         "windows": [{"nokey": 1}]}]},  # key が str でない
        {"cached_at": "not a date", "services": []},                   # cached_at 不正
        {"services": []},                                              # cached_at なし
        {"cached_at": iso, "fetched_from": "oops", "services": []},    # fetched_from 不正
    ]
    for key in ("name", "source", "plan", "error"):                    # 任意項目も str|null のみ
        invalid.append({"cached_at": iso, "services":
                        [_entry("x", **{key: 1})]})
    invalid.append({"cached_at": iso, "services":
                    [_entry("x", windows=[{"key": "w", "label": 2}])]})  # label も str|null
    for bad in invalid:
        path.write_text(json.dumps(bad), encoding="utf-8")
        assert detect_cache.read_cache() is None, bad
    # 任意項目がすべて null / 省略なら有効
    ok = {"cached_at": iso, "services": [_entry("x")]}
    path.write_text(json.dumps(ok), encoding="utf-8")
    assert detect_cache.read_cache() == ok


def test_detect_cached_invalid_structure_falls_back(monkeypatch, capsys, tmp_path):
    path = _patch_path(monkeypatch, tmp_path)
    path.write_text(json.dumps({"cached_at": "2026-10-25T18:00:00+09:00",
                                "services": [{}]}), encoding="utf-8")
    services = [_entry("claude")]
    monkeypatch.setattr(providers, "detect_all", lambda now=None: services)
    rc, out = _run(monkeypatch, capsys, ["detect", "--json", "--cached"])
    assert rc == 0
    assert json.loads(out)["services"] == services     # 全取得経路に落ちて成功する


# ------------------------------------------------ 常駐側の更新 (周回の usages)

def _win(pid, key, part_of=None):
    return Window(pid, key, key, 10.0, NOW + timedelta(days=1), timedelta(days=7),
                  part_of=part_of)


def _provider(pid, fetched):
    """fetch が呼ばれたら記録して失敗する provider (追加取得の検出用)。
    detect() は資格情報ありのローカル確認だけを返す。"""
    def fetch(now):
        fetched.append(pid)
        raise AssertionError("キャッシュ更新では取得しない")
    return Provider(pid, pid.title(), True, lambda: Detection(True, "keychain"), fetch)


def test_update_detect_cache_uses_usages_without_fetching(monkeypatch, tmp_path):
    path = _patch_path(monkeypatch, tmp_path)
    fetched = []
    monkeypatch.setattr(providers, "REGISTRY", {
        "claude": _provider("claude", fetched),
        "glm": _provider("glm", fetched),
    })
    usage = ProviderUsage(
        "claude", "Claude Max",
        [_win("claude", "5h"),
         _win("claude", "weekly"), _win("claude", "weekly"),  # 重複 key は除く
         _win("claude", "weekly_fable", part_of="weekly")],  # 内訳窓も除く
        None, NOW)
    cli._update_detect_cache([usage], NOW)
    assert fetched == []                               # 追加の取得はしない
    data = json.loads(path.read_text(encoding="utf-8"))
    by_pid = {e["provider"]: e for e in data["services"]}
    assert by_pid["claude"]["plan"] == "Max"
    assert [w["key"] for w in by_pid["claude"]["windows"]] == ["5h", "weekly"]
    assert by_pid["claude"]["error"] is None
    # usages に無い provider は detect() の結果だけ (窓なし・エラーなし)
    assert by_pid["glm"]["windows"] == [] and by_pid["glm"]["error"] is None

    # 2 回目: usages に無い provider は前回キャッシュの plan / windows を残す
    cli._update_detect_cache([], NOW + timedelta(minutes=15))
    assert fetched == []
    data = json.loads(path.read_text(encoding="utf-8"))
    by_pid = {e["provider"]: e for e in data["services"]}
    assert by_pid["claude"]["plan"] == "Max"
    assert [w["key"] for w in by_pid["claude"]["windows"]] == ["5h", "weekly"]


def test_update_detect_cache_reports_usage_error(monkeypatch, tmp_path):
    path = _patch_path(monkeypatch, tmp_path)
    fetched = []
    monkeypatch.setattr(providers, "REGISTRY", {"glm": _provider("glm", fetched)})
    usage = ProviderUsage("glm", "GLM", [], "HTTP 429", NOW)
    cli._update_detect_cache([usage], NOW)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["services"][0]["error"] == "HTTP 429"
    assert data["services"][0]["windows"] == []
    assert fetched == []


def test_update_detect_cache_marks_stale_usage_as_error(monkeypatch, tmp_path):
    """取得失敗中の前回成功値 (stale_error 付き) は plan/windows を残しつつ
    error に失敗理由を入れる (正常値としてキャッシュしない)。"""
    path = _patch_path(monkeypatch, tmp_path)
    fetched = []
    monkeypatch.setattr(providers, "REGISTRY", {"claude": _provider("claude", fetched)})
    usage = ProviderUsage("claude", "Claude Max", [_win("claude", "weekly")],
                          None, NOW, stale_error="HTTP 500")
    cli._update_detect_cache([usage], NOW)
    entry = json.loads(path.read_text(encoding="utf-8"))["services"][0]
    assert entry["error"] == "HTTP 500"
    assert entry["plan"] == "Max"
    assert [w["key"] for w in entry["windows"]] == ["weekly"]


# ------------------------------------------------------- fetched_from 巻き戻し防止

def test_write_cache_compares_per_provider(tmp_path):
    """provider 単位で「新しい方」を採る: 古い取得からの書き込みは同じ
    provider の新しいエントリを巻き戻せないが、別 provider は足せる。"""
    path = tmp_path / "detect-cache.json"
    newer = NOW + timedelta(minutes=10)
    assert detect_cache.write_cache([_entry("a")], newer, path=path)
    # 別 provider のエントリは採り込まれる (a は新しい方を保持)
    assert detect_cache.write_cache([_entry("b")], NOW, path=path)
    by_pid = {e["provider"]: e
              for e in json.loads(path.read_text(encoding="utf-8"))["services"]}
    assert by_pid["a"]["fetched_from"] == detect_cache._iso_us(newer)
    assert by_pid["b"]["fetched_from"] == detect_cache._iso_us(NOW)
    # 同じ provider を古い時刻で上書きしようとしても採られない (変化なしで False)
    assert not detect_cache.write_cache([_entry("a", plan="Old")], NOW, path=path)
    by_pid = {e["provider"]: e
              for e in json.loads(path.read_text(encoding="utf-8"))["services"]}
    assert by_pid["a"]["plan"] is None                  # 新しい方のまま


def test_write_cache_compares_fetched_from_subsecond(tmp_path):
    """fetched_from はマイクロ秒精度で比較する: 同じ秒でも新しい取得を
    古い取得が巻き戻さない。"""
    path = tmp_path / "detect-cache.json"
    newer = NOW + timedelta(milliseconds=500)          # NOW と同じ秒の後半
    assert detect_cache.write_cache([_entry("a")], newer, path=path)
    assert not detect_cache.write_cache([_entry("a", plan="Old")], NOW, path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["services"][0]
    assert entry["plan"] is None                        # 新しい方のまま
    assert detect_cache._parse_iso(entry["fetched_from"]).microsecond == 500000
    later_same_second = NOW + timedelta(milliseconds=900)
    assert detect_cache.write_cache([_entry("a", plan="New")], later_same_second,
                                    path=path)


def test_write_cache_merges_previous_inside_lock(tmp_path):
    """merge はロック内で読み直した有効な前回キャッシュ (エントリごとの
    fetched_from 入り) に対して呼ばれる。"""
    path = tmp_path / "detect-cache.json"
    detect_cache.write_cache([_entry("a")], NOW, path=path)
    seen = []

    def merge(previous):
        seen.append(previous)
        return list(previous or []) + [_entry("b")]

    assert detect_cache.write_cache(None, NOW + timedelta(minutes=1),
                                    path=path, merge=merge)
    # merge が受け取るのはロック内の読み直し + 時刻正規化済みの前回値
    assert seen == [[dict(_entry("a"), fetched_from=detect_cache._iso_us(NOW))]]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [e["provider"] for e in data["services"]] == ["a", "b"]
    # 変化なしの書き込みは置換しない
    def merge_noop(previous):
        return list(previous or [])
    assert not detect_cache.write_cache(None, NOW - timedelta(minutes=1),
                                        path=path, merge=merge_noop)
    assert [e["provider"] for e in
            json.loads(path.read_text(encoding="utf-8"))["services"]] == ["a", "b"]


def test_update_detect_cache_does_not_rollback_fresher_detect(monkeypatch, tmp_path):
    """資格情報変更後の detect (新しい fetched_from) を、遅れて完了した常駐周回
    (古い fetched_from) が巻き戻さない。"""
    path = _patch_path(monkeypatch, tmp_path)
    monkeypatch.setattr(providers, "REGISTRY", {})
    newer = NOW + timedelta(minutes=10)
    detect_cache.write_cache([_entry("claude", plan="Max")], newer)
    cli._update_detect_cache([], NOW)                   # 古い周回が後から到着した想定
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [{k: v for k, v in e.items() if k != "fetched_from"}
            for e in data["services"]] == [_entry("claude", plan="Max")]
    assert data["fetched_from"] == detect_cache._iso_us(newer)


def test_resident_round_keeps_unfetched_provider_entry(monkeypatch, tmp_path):
    """常駐周回は実取得した provider だけ自分の時刻で更新し、取得していない
    provider は既存エントリを時刻ごとそのまま残す。"""
    path = _patch_path(monkeypatch, tmp_path)
    fetched = []
    monkeypatch.setattr(providers, "REGISTRY", {
        "claude": _provider("claude", fetched),
        "glm": _provider("glm", fetched),
    })
    later = NOW + timedelta(minutes=10)
    # 手動 detect が先に glm の実データを書いている
    detect_cache.write_cache(
        [dict(_entry("glm", plan="Pro",
                     windows=[{"key": "weekly", "label": "週間"}]),
              fetched_from=detect_cache._iso_us(NOW))], NOW)
    usage = ProviderUsage("claude", "Claude Max", [_win("claude", "weekly")],
                          None, later)
    cli._update_detect_cache([usage], later)             # glm は usages に無い
    by_pid = {e["provider"]: e
              for e in json.loads(path.read_text(encoding="utf-8"))["services"]}
    assert by_pid["glm"]["plan"] == "Pro"
    assert by_pid["glm"]["fetched_from"] == detect_cache._iso_us(NOW)  # 時刻も保持
    assert by_pid["claude"]["fetched_from"] == detect_cache._iso_us(later)
    assert fetched == []


def test_late_manual_detect_keeps_unfetched_provider_results(monkeypatch, tmp_path):
    """手動 detect (t1) の書き込みが、途中で始まった常駐取得 (t2>t1) の書き込みの
    あとに完了しても、常駐が取得しない provider の手動結果は provider 単位の
    「新しい方」で残る (常駐が実取得した provider は常駐側が新しい)。"""
    path = _patch_path(monkeypatch, tmp_path)
    fetched = []
    monkeypatch.setattr(providers, "REGISTRY", {
        "claude": _provider("claude", fetched),
        "glm": _provider("glm", fetched),
    })
    t1, t2 = NOW, NOW + timedelta(seconds=5)
    # t2 に始まった常駐周回が先に書く (claude だけ取得、glm は未取得)
    usage = ProviderUsage("claude", "Claude Max", [_win("claude", "weekly")], None, t2)
    cli._update_detect_cache([usage], t2)
    # t1 に始まった手動 detect が後から完了: glm の実取得結果も持つ
    detect_cache.write_cache(
        [_entry("claude", plan="Max"),
         _entry("glm", plan="Pro", windows=[{"key": "weekly", "label": "週間"}])], t1)
    by_pid = {e["provider"]: e
              for e in json.loads(path.read_text(encoding="utf-8"))["services"]}
    assert by_pid["claude"]["fetched_from"] == detect_cache._iso_us(t2)  # 常駐側が新しい
    assert by_pid["glm"]["plan"] == "Pro"                                 # 手動結果が残る
    assert by_pid["glm"]["fetched_from"] == detect_cache._iso_us(t1)


def test_update_detect_cache_failure_does_not_raise(monkeypatch, tmp_path, caplog):
    """書き込みに失敗しても常駐ループは止めない (ログだけ)。"""
    missing_dir = tmp_path / "no-such-dir"
    monkeypatch.setattr(detect_cache, "DETECT_CACHE_PATH", missing_dir)
    monkeypatch.setattr(providers, "REGISTRY", {})
    # 存在しないパス名 (親作成も失敗する) を返させる
    monkeypatch.setattr(detect_cache, "write_cache",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    cli._update_detect_cache([], NOW)                  # 例外を投げない
    assert "detect-cache.json" in caplog.text
