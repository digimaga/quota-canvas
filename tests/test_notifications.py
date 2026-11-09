from datetime import datetime, timezone
import json

import pytest
from quotacanvas.notifications import plan
from quotacanvas import cli

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RESET = '2026-01-02T00:00:00+00:00'
CARD = {'id': 'glm:5h', 'provider': 'glm', 'name': 'GLM', 'label': '5h', 'status': 'over', 'resets_at': RESET}
CREDIT = {'title': 'reset', 'expires_at': '2026-01-02T00:00:00+00:00'}


def test_first_snapshot_only_credit_and_key_matches_swift():
    current = {'cards': [CARD], 'reset_credits': {'glm': [CREDIT]}}
    candidates = plan(None, current, NOW, set())
    assert [(c['kind'], c['key']) for c in candidates] == [('credit', 'credit|glm|reset|' + CREDIT['expires_at'])]
    assert candidates[0]['name'] == 'GLM'
    assert plan(None, current, NOW, {candidates[0]['key']}) == []


def test_transition_keys_tolerance_bucket_and_deduplication():
    previous = {'cards': [{**CARD, 'status': 'go', 'resets_at': '2026-01-01T23:58:59+00:00'}]}
    current = {'cards': [{**CARD, 'burst_exhausts_at': '2026-01-01T10:00:00+00:00'}]}
    candidates = plan(previous, current, NOW, set())
    period = int(datetime.fromisoformat(RESET).timestamp() / 300)
    assert [(c['kind'], c['key']) for c in candidates] == [
        ('status', f'status|glm:5h|{period}|over'),
        ('reset', f'reset|glm:5h|{period}'),
        ('burst', f'burst|glm:5h|{period}')]
    assert plan(previous, current, NOW, {c['key'] for c in candidates}) == []
    previous['cards'][0]['resets_at'] = '2026-01-01T23:59:00+00:00'
    assert [c['kind'] for c in plan(previous, current, NOW, set())] == ['status', 'burst']


def test_same_status_new_period_and_fallback_id():
    current = {'cards': [{'provider': '', 'label': '', 'status': 'exhausted', 'resets_at': RESET}]}
    candidates = plan({'cards': []}, current, NOW, set())
    assert candidates[0]['key'].startswith('status|||')
    assert plan(current, current, NOW, set()) == []


@pytest.mark.parametrize('expires, count', [
    ('2026-01-01T00:00:00+00:00', 0), ('2026-01-02T00:00:00+00:00', 1),
    ('2026-01-02T00:00:01+00:00', 0), ('bad', 0)])
def test_credit_window(expires, count):
    assert len(plan(None, {'reset_credits': {'glm': [{'expires_at': expires}]}}, NOW, set())) == count


def test_notifications_cli(monkeypatch, capsys, tmp_path):
    import io
    payload = {'previous': None, 'current': {'reset_credits': {'glm': [CREDIT]}},
               'now': NOW.isoformat(), 'notified': []}
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    assert cli.main(['--config', str(tmp_path / 'config.toml'), 'notifications-plan']) == 0
    assert json.loads(capsys.readouterr().out)['candidates'][0]['kind'] == 'credit'


def test_legacy_swift_planner_fixture():
    # Keys captured from the original Swift NotificationPlanner for these snapshots.
    previous = {'cards': [{**CARD, 'status': 'go', 'resets_at': '2026-01-01T23:58:59+00:00'}]}
    current = {'cards': [{**CARD, 'burst_exhausts_at': '2026-01-01T10:00:00+00:00'}],
               'reset_credits': {'glm': [CREDIT]}}
    credit = 'credit|glm|reset|2026-01-02T00:00:00+00:00'
    assert [c['key'] for c in plan(None, current, NOW, set())] == [credit]
    assert [c['key'] for c in plan(previous, current, NOW, set())] == [
        'status|glm:5h|5891040|over', 'reset|glm:5h|5891040',
        'burst|glm:5h|5891040', credit]


def test_malformed_credits_field_matches_swift_optional_decode():
    current = {'reset_credits': {'glm': [CREDIT], 'codex': 'invalid'}}
    assert plan(None, current, NOW, set()) == []


def test_submillisecond_credit_boundary():
    expires = NOW.isoformat()
    status = {'reset_credits': {'glm': [{'expires_at': expires}]}}
    assert len(plan(None, status, NOW.timestamp() - 0.0001, set())) == 1
    assert plan(None, status, NOW.timestamp(), set()) == []


def test_estimated_reset_replaced_by_api_time_is_not_notified_again():
    # リセット済みとみなした周回 (推定時刻) → API の実際の時刻: リセット通知は 1 回だけ
    old = {**CARD, 'status': 'go', 'resets_at': '2026-01-01T00:00:00+00:00'}
    estimated = {**CARD, 'status': 'fresh', 'resets_at': '2026-01-08T00:00:00+00:00',
                 'reset_estimated': True}
    actual = {**CARD, 'status': 'fresh', 'resets_at': '2026-01-08T00:20:00+00:00'}
    first = plan({'cards': [old]}, {'cards': [estimated]}, NOW, set())
    assert [c['kind'] for c in first] == ['reset']
    assert plan({'cards': [estimated]}, {'cards': [actual]}, NOW, set()) == []


def test_estimated_reset_already_passed_still_notifies_next_period():
    # 推定した次回時刻も過ぎた (長いスリープ後など) なら、次の期間へのリセットは通知する
    estimated = {**CARD, 'status': 'fresh', 'resets_at': '2025-12-31T00:00:00+00:00',
                 'reset_estimated': True}
    actual = {**CARD, 'status': 'fresh', 'resets_at': '2026-01-07T00:00:00+00:00'}
    assert [c['kind'] for c in plan({'cards': [estimated]}, {'cards': [actual]}, NOW, set())] == ['reset']
