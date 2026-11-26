"""GLM (Z.ai) のリセット権 (Reset Card) の一覧取得。表示専用で、使う要求は送らない。"""

from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas.providers import glm

NOW = datetime(2026, 11, 26, 8, 0, tzinfo=timezone.utc)


def _payload(five=(), week=()):
    return {"code": 200, "success": True, "data": {
        "targetType": "PERSONAL",
        "fiveHourResets": list(five),
        "weekResets": list(week),
    }}


def _card(expire, available=True):
    return {"recordId": 1, "grantType": "GLOBAL", "expireTime": expire, "available": available}


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(glm, "_RESET_CACHE", {"at": None, "credits": [], "key_id": None})


def test_parse_orders_by_expiry_and_reads_utc8():
    data = _payload(five=[_card("2026-12-22 23:59:59")], week=[_card("2026-12-01 08:00:00")])
    credits = glm.parse_glm_reset_credits(data, NOW)
    assert [c.title for c in credits] == ["週間枠リセット", "5時間枠リセット"]
    # 中国標準時 (UTC+8) として読む
    assert credits[1].expires_at == datetime(2026, 12, 22, 15, 59, 59, tzinfo=timezone.utc)


def test_parse_excludes_unavailable_and_expired():
    data = _payload(five=[_card("2026-12-22 23:59:59", available=False),
                          _card("2026-11-26 15:00:00"),          # = 07:00 UTC, 期限切れ
                          _card("2026-11-26 17:00:00"),          # = 09:00 UTC, 有効
                          _card("bad")])                          # 日時不明は残す
    credits = glm.parse_glm_reset_credits(data, NOW)
    assert len(credits) == 2
    assert credits[0].expires_at == datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)
    assert credits[1].expires_at is None


def test_parse_junk():
    assert glm.parse_glm_reset_credits({}, NOW) == []
    assert glm.parse_glm_reset_credits({"data": None}, NOW) == []
    assert glm.parse_glm_reset_credits({"data": {"fiveHourResets": [None, "x"]}}, NOW) == []


def test_fetch_attaches_credits_and_caches(monkeypatch):
    calls = []

    def fake_http(url, headers, timeout=None):
        calls.append(url)
        if url == glm.GLM_USAGE_URL:
            return {"data": {"limits": [], "level": "pro"}}
        assert url == glm.GLM_RESET_CREDITS_URL
        assert headers["Authorization"] == "k"
        return _payload(five=[_card("2026-12-22 23:59:59"), _card("2026-12-22 23:59:59")])

    monkeypatch.setattr(glm, "http_get_json", fake_http)
    monkeypatch.setattr(glm, "read_glm_api_key", lambda: "k")
    usage = glm.fetch(NOW)
    assert len(usage.reset_credits) == 2
    glm.fetch(NOW + timedelta(minutes=5))
    assert calls.count(glm.GLM_RESET_CREDITS_URL) == 1        # 15 分以内は取り直さない
    glm.fetch(NOW + timedelta(minutes=16))
    assert calls.count(glm.GLM_RESET_CREDITS_URL) == 2


def test_fetch_failure_keeps_usage_and_previous(monkeypatch):
    state = {"fail": False}

    def fake_http(url, headers, timeout=None):
        if url == glm.GLM_USAGE_URL:
            return {"data": {"limits": [], "level": "pro"}}
        if state["fail"]:
            raise OSError("down")
        return _payload(five=[_card("2026-12-22 23:59:59")])

    monkeypatch.setattr(glm, "http_get_json", fake_http)
    monkeypatch.setattr(glm, "read_glm_api_key", lambda: "k")
    assert len(glm.fetch(NOW).reset_credits) == 1
    state["fail"] = True
    later = NOW + timedelta(minutes=20)
    assert len(glm.fetch(later).reset_credits) == 1            # 前回の結果
    monkeypatch.setattr(glm, "read_glm_api_key", lambda: "other")
    assert glm.fetch(later).reset_credits == []                # 別キーの件は返さない


def test_error_body_is_failure(monkeypatch):
    monkeypatch.setattr(glm, "http_get_json",
                        lambda url, headers, timeout=None: {"code": 400, "success": False})
    assert glm.fetch_reset_credits(NOW, "k") == []
    monkeypatch.setattr(glm, "http_get_json",
                        lambda url, headers, timeout=None: {"code": 500, "data": {}})
    assert glm.fetch_reset_credits(NOW, "k") == []
    assert glm._RESET_CACHE["at"] is None                     # 失敗はキャッシュしない


def test_module_never_calls_use_endpoint():
    import inspect
    assert "customer-package-reset/use" not in inspect.getsource(glm)
