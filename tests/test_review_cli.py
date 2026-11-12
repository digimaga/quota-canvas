"""cli 担当レビュー指摘の回帰テスト。

- turzx.Screen: USB 初期化の例外を RuntimeError に正規化、brightness の結果伝搬
- turzx cli: show の失敗・brightness の失敗を簡潔なエラー + exit 1 に
- quotacanvas.cli: _apply_brightness / _sleep_screen は応答を確認してから
  applied_brightness / dimmed を更新する、_status_recommendation の未来時刻
  許容、_write_app_files が OSError 以外の失敗でも常駐を続ける
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import usb.core
from PIL import Image

from quotacanvas import cli
from quotacanvas.config import brightness_for, load_config
from turzx import cli as tcli
from turzx import screen as tscreen

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------- turzx.Screen

def _screen_without_usb(monkeypatch, dev):
    """USB を触らずに Screen を作る (find を dev に差し替える)。"""
    monkeypatch.setattr(tscreen, "_backend", lambda: object())
    monkeypatch.setattr(tscreen.usb.core, "find", lambda **kwargs: dev)
    return tscreen.Screen()


def test_screen_init_normalizes_usb_error_during_enumeration(monkeypatch):
    """usb.core.find の列挙で USBError (OSError 系) が出ても RuntimeError に揃う。
    常駐側 _open_screen は RuntimeError だけを見る。"""
    monkeypatch.setattr(tscreen, "_backend", lambda: object())

    def boom(**kwargs):
        raise usb.core.USBError("enumeration failed")

    monkeypatch.setattr(tscreen.usb.core, "find", boom)
    with pytest.raises(RuntimeError):
        tscreen.Screen()


def test_screen_init_normalizes_value_error(monkeypatch):
    """find / set_configuration の ValueError も RuntimeError に揃う。"""

    def boom(**kwargs):
        raise ValueError("invalid configuration value")

    monkeypatch.setattr(tscreen, "_backend", lambda: object())
    monkeypatch.setattr(tscreen.usb.core, "find", boom)
    with pytest.raises(RuntimeError):
        tscreen.Screen()

    class Dev:
        def set_configuration(self):
            raise ValueError("no configuration")

    with pytest.raises(RuntimeError):
        _screen_without_usb(monkeypatch, Dev())


def test_screen_init_still_ignores_configured_usb_error(monkeypatch):
    """set_configuration の USBError は従来どおり「設定済み」として無視する。"""

    class Dev:
        def set_configuration(self):
            raise usb.core.USBError("already configured")

    screen = _screen_without_usb(monkeypatch, Dev())
    assert screen.dev is not None


def test_screen_init_not_found_still_runtime_error(monkeypatch):
    """デバイス未検出 (find=None) は従来どおり RuntimeError。"""
    monkeypatch.setattr(tscreen, "_backend", lambda: object())
    monkeypatch.setattr(tscreen.usb.core, "find", lambda **kwargs: None)
    with pytest.raises(RuntimeError):
        tscreen.Screen()


def test_screen_brightness_returns_send_result(monkeypatch):
    """brightness は送信結果を返す: 応答なし (None) は False。"""
    screen = _screen_without_usb(monkeypatch, SimpleNamespace(set_configuration=lambda: None))
    monkeypatch.setattr(tscreen.operations, "send_brightness_command",
                        lambda dev, v: b"ok")
    assert screen.brightness(50) is True
    monkeypatch.setattr(tscreen.operations, "send_brightness_command",
                        lambda dev, v: None)
    assert screen.brightness(50) is False


# ---------------------------------------------------------------- turzx cli

def test_turzx_show_missing_file_is_concise_error_exit1(monkeypatch, capsys, tmp_path):
    """存在しない画像の show は traceback ではなく簡潔なエラー + exit 1。"""
    screen = object.__new__(tscreen.Screen)  # USB を触らずに実 show を使う
    screen.rotation = tscreen.DEFAULT_ROTATION
    screen.dev = None
    monkeypatch.setattr(tcli, "Screen", lambda rotation=None: screen)
    missing = tmp_path / "missing.png"
    assert tcli.main(["show", str(missing)]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and err.strip()


def test_turzx_brightness_failure_exits_1(monkeypatch, capsys):
    """明るさの送信に応答が無ければ (None) 成功扱いせず exit 1。"""
    screen = SimpleNamespace(brightness=lambda value: False)
    monkeypatch.setattr(tcli, "Screen", lambda rotation=None: screen)
    assert tcli.main(["brightness", "50"]) == 1
    assert capsys.readouterr().err.strip()


def test_turzx_brightness_success_exits_0(monkeypatch):
    screen = SimpleNamespace(brightness=lambda value: True)
    monkeypatch.setattr(tcli, "Screen", lambda rotation=None: screen)
    assert tcli.main(["brightness", "50"]) == 0


def test_turzx_screen_open_failure_is_concise_error_exit1(monkeypatch, capsys):
    """USB 初期化の失敗 (正規化済みの RuntimeError) は簡潔なエラー + exit 1。"""

    def boom(rotation=None):
        raise RuntimeError("cannot open TURZX screen (1cbe:0092): enumeration failed")

    monkeypatch.setattr(tcli, "Screen", boom)
    assert tcli.main(["sync"]) == 1
    err = capsys.readouterr().err
    assert "enumeration failed" in err and "Traceback" not in err


# ------------------------------------------------- quotacanvas.cli の明るさ

def test_apply_brightness_retries_when_screen_does_not_ack(caplog):
    """brightness が False (応答なし) なら applied を更新せず次の周回で再試行。"""
    cfg = load_config(None)
    screen = SimpleNamespace(calls=[], brightness=None)

    def brightness(value):
        screen.calls.append(value)
        return False

    screen.brightness = brightness
    want = brightness_for(cfg)  # 夜間帯なら night.brightness (実行時刻に依存させない)
    assert cli._apply_brightness(screen, cfg, None) is None  # 適用済みにしない
    assert cli._apply_brightness(screen, cfg, None) is None  # 次の周回で再試行する
    assert screen.calls == [want, want]


def test_apply_brightness_marks_applied_on_success(caplog):
    cfg = load_config(None)
    screen = SimpleNamespace(calls=[],
                             brightness=lambda v: screen.calls.append(v) or True)
    want = brightness_for(cfg)  # 夜間帯なら night.brightness (実行時刻に依存させない)
    assert cli._apply_brightness(screen, cfg, None) == want
    # 適用済みなら同じ周回以降は送らない
    assert cli._apply_brightness(screen, cfg, want) == want
    assert screen.calls == [want]


def test_sleep_screen_false_when_brightness_zero_not_acked(caplog):
    """brightness(0) が False を返しても dimmed を立てさせない (False を返す)。"""
    screen = SimpleNamespace(brightness=lambda v: False)
    assert cli._sleep_screen(screen) is False


# ------------------------------------------- _status_recommendation の未来時刻

def _status_file(tmp_path, updated_at):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"updated_at": updated_at.isoformat(),
                                "recommendation": {"text": "use glm"}}),
                    encoding="utf-8")
    return path


def test_status_recommendation_far_future_updated_at_is_stale(tmp_path):
    """updated_at が許容誤差 (2 分) を超えて未来なら古い扱い (False, None)。
    時計ずれや他の Mac からのコピーで、updated_at + 20 分まで新鮮扱いにしない。"""
    path = _status_file(tmp_path, NOW + timedelta(minutes=10))
    assert cli._status_recommendation(NOW, path) == (False, None)


def test_status_recommendation_small_future_skew_is_fresh(tmp_path):
    """2 分以内の小さな未来ずれは許す (時計の微妙なずれ)。"""
    path = _status_file(tmp_path, NOW + timedelta(minutes=1))
    assert cli._status_recommendation(NOW, path) == (True, {"text": "use glm"})


def test_status_recommendation_past_20min_is_still_stale(tmp_path):
    path = _status_file(tmp_path, NOW - timedelta(minutes=25))
    assert cli._status_recommendation(NOW, path) == (False, None)


# --------------------------------------------------------- _write_app_files

def test_write_app_files_continues_on_non_oserror(tmp_path, monkeypatch, caplog):
    """status.json 側の失敗が OSError 以外 (NaN の ValueError など) でも
    例外を上げず、後続の frame.png 書き込みも続ける (desktop_frame.update
    と同じ扱い)。"""
    frames = []

    def boom(payload, *a, **k):
        raise ValueError("out of range float values are not JSON compliant")

    monkeypatch.setattr(cli, "write_status", boom)
    monkeypatch.setattr(cli, "write_frame", lambda img, *a, **k: frames.append(img))
    monkeypatch.setattr(cli, "desktop_frame", SimpleNamespace(update=lambda fn: None))
    cfg = load_config(None)
    cli._write_app_files(cfg, [], [], NOW, Image.new("RGB", (8, 8)))
    assert frames  # status の失敗で frame.png も止まらない


def test_restored_backoff_is_capped_at_backoff_max(tmp_path):
    """保存された待ちが BACKOFF_MAX より先でも、復元時は BACKOFF_MAX に丸める
    (時計の巻き戻しや手書きのファイルで、取得が長く止まらないように)。"""
    import json as _json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from quotacanvas import cli as _cli

    path = tmp_path / "rate-limit.json"
    far = _dt.now(_tz.utc) + _td(days=3)
    path.write_text(_json.dumps({"claude": {"until": far.isoformat(), "count": 2}}),
                    encoding="utf-8")
    poller = _cli.Poller.__new__(_cli.Poller)
    poller._backoff_path = path
    poller._next_attempt = {}
    poller._rate_limited = {}
    poller._load_backoff()
    restored = poller._next_attempt["claude"]
    assert restored <= _dt.now(_tz.utc) + _cli.BACKOFF_MAX
    assert poller._rate_limited["claude"] == 2
