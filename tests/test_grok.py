from datetime import datetime, timedelta, timezone

from quotacanvas.providers.grok import parse_grok

# Shape of cli-chat-proxy.grok.com/v1/billing?format=credits (2026-10-24)
SAMPLE = {"config": {
    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                      "start": "2026-10-19T01:45:37.407211+00:00",
                      "end": "2026-10-26T01:45:37.407211+00:00"},
    "creditUsagePercent": 22.0,
    "productUsage": [{"product": "GrokBuild", "usagePercent": 20.0},
                     {"product": "GrokTasks", "usagePercent": 2.0}],
    "billingPeriodStart": "2026-10-19T01:45:37.407211+00:00",
    "billingPeriodEnd": "2026-10-26T01:45:37.407211+00:00",
}}

RESET = datetime(2026, 10, 26, 1, 45, 37, 407211, tzinfo=timezone.utc)


def test_parse_grok_weekly():
    name, windows = parse_grok(SAMPLE)
    assert name == "Grok"
    (w, build, tasks) = windows
    assert (w.id, w.label, w.used_percent) == ("grok:weekly", "週間", 22.0)
    assert w.length == timedelta(days=7)
    assert w.resets_at == RESET
    assert w.part_of is None
    # productUsage はメイン窓の内訳 (part_of 付き) になる
    assert (build.id, build.label, build.used_percent) == \
        ("grok:weekly_build", "週間 Build", 20.0)
    assert (tasks.id, tasks.label, tasks.used_percent) == \
        ("grok:weekly_tasks", "週間 Tasks", 2.0)
    for sub in (build, tasks):
        assert sub.part_of == "weekly"
        assert sub.resets_at == w.resets_at and sub.length == w.length


def test_parse_grok_missing_period():
    assert parse_grok({"config": {"creditUsagePercent": 5}}) == ("Grok", [])
    assert parse_grok({}) == ("Grok", [])


def test_parse_grok_zero_usage_omitted():
    """リセット直後は creditUsagePercent (0) が応答から省かれる。0% の窓にする。"""
    cfg = {k: v for k, v in SAMPLE["config"].items()
           if k not in ("creditUsagePercent", "productUsage")}
    name, windows = parse_grok({"config": cfg})
    (w,) = windows
    assert (w.id, w.used_percent, w.resets_at) == ("grok:weekly", 0.0, RESET)


def test_parse_grok_product_usage_variants():
    """productUsage 無し・壊れていてもメイン窓だけで例外を出さない。"""
    base = {"currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-10-19T01:45:37+00:00",
                "end": "2026-10-26T01:45:37+00:00"},
            "creditUsagePercent": 22.0}

    # 無し
    _name, windows = parse_grok({"config": dict(base)})
    assert [w.id for w in windows] == ["grok:weekly"]
    assert all(w.part_of is None for w in windows)

    # 要素が壊れている (型違い / product 無し / usagePercent 無し)
    for products in ("x", 3, [None, "x", 3],
                     [{"usagePercent": 1.0}],
                     [{"product": "GrokBuild"}],
                     [{"product": "GrokBuild", "usagePercent": "20"}],
                     [{"product": "", "usagePercent": 1.0}]):
        cfg = dict(base)
        cfg["productUsage"] = products
        _name, windows = parse_grok({"config": cfg})
        assert [w.id for w in windows] == ["grok:weekly"], products


def test_parse_grok_product_usage_zero_or_negative_dropped():
    """usagePercent <= 0 の製品は内訳にしない (0 より大きいものだけ残す)。"""
    cfg = {"currentPeriod": {
               "type": "USAGE_PERIOD_TYPE_WEEKLY",
               "start": "2026-10-19T01:45:37+00:00",
               "end": "2026-10-26T01:45:37+00:00"},
           "creditUsagePercent": 22.0,
           "productUsage": [{"product": "GrokBuild", "usagePercent": 20.0},
                            {"product": "GrokIdle", "usagePercent": 0.0},
                            {"product": "GrokNeg", "usagePercent": -1.5}]}
    _name, windows = parse_grok({"config": cfg})
    assert [w.id for w in windows] == ["grok:weekly", "grok:weekly_build"]


def test_parse_grok_product_name_without_grok_prefix():
    """先頭 "Grok" を外した結果が空のときは product 全体を使う。"""
    cfg = {"currentPeriod": {
               "type": "USAGE_PERIOD_TYPE_WEEKLY",
               "start": "2026-10-19T01:45:37+00:00",
               "end": "2026-10-26T01:45:37+00:00"},
           "creditUsagePercent": 5.0,
           "productUsage": [{"product": "Grok", "usagePercent": 1.0},
                            {"product": "API", "usagePercent": 2.0}]}
    _name, windows = parse_grok({"config": cfg})
    assert [w.id for w in windows] == \
        ["grok:weekly", "grok:weekly_grok", "grok:weekly_api"]
    assert [w.label for w in windows] == ["週間", "週間 Grok", "週間 API"]


def test_parse_grok_negative_usage_dropped():
    """usagePercent が負の製品は内訳に入れない。"""
    cfg = {"currentPeriod": {
               "type": "USAGE_PERIOD_TYPE_WEEKLY",
               "start": "2026-10-19T01:45:37+00:00",
               "end": "2026-10-26T01:45:37+00:00"},
           "creditUsagePercent": 5.0,
           "productUsage": [{"product": "GrokBuild", "usagePercent": -3.0},
                            {"product": "GrokTasks", "usagePercent": 2.0}]}
    _name, windows = parse_grok({"config": cfg})
    assert [w.key for w in windows] == ["weekly", "weekly_tasks"]
    assert windows[1].used_percent == 2.0


def test_parse_grok_breakdown_truncated_to_main_used():
    """内訳の累計はメイン窓の使用率を超えない。超える分は切り詰め、0 に
    なった製品は捨てる。"""
    cfg = {"currentPeriod": {
               "type": "USAGE_PERIOD_TYPE_WEEKLY",
               "start": "2026-10-19T01:45:37+00:00",
               "end": "2026-10-26T01:45:37+00:00"},
           "creditUsagePercent": 22.0,
           "productUsage": [{"product": "GrokBuild", "usagePercent": 15.0},
                            {"product": "GrokTasks", "usagePercent": 10.0},
                            {"product": "GrokSearch", "usagePercent": 3.0}]}
    _name, windows = parse_grok({"config": cfg})
    assert [(w.key, w.used_percent) for w in windows] == [
        ("weekly", 22.0),
        ("weekly_build", 15.0),
        ("weekly_tasks", 7.0),      # 22 - 15 に切り詰め
    ]                               # GrokSearch は 0 になるので出ない


def test_parse_grok_duplicate_normalized_key_keeps_first():
    """正規化後の key が重なる製品 ("GrokBuild" と "Build" -> weekly_build)
    は後のものを捨てる。"""
    cfg = {"currentPeriod": {
               "type": "USAGE_PERIOD_TYPE_WEEKLY",
               "start": "2026-10-19T01:45:37+00:00",
               "end": "2026-10-26T01:45:37+00:00"},
           "creditUsagePercent": 22.0,
           "productUsage": [{"product": "GrokBuild", "usagePercent": 12.0},
                            {"product": "Build", "usagePercent": 5.0}]}
    _name, windows = parse_grok({"config": cfg})
    assert [(w.key, w.used_percent) for w in windows] == [
        ("weekly", 22.0), ("weekly_build", 12.0)]


def test_parse_grok_weekly_label_english():
    """en のとき窓ラベルは Weekly になる。"""
    from quotacanvas import i18n

    i18n.set_language("en")
    try:
        _, windows = parse_grok(SAMPLE)
        assert windows[0].label == "Weekly"
        assert windows[1].label == "Weekly Build"
    finally:
        i18n.set_language("ja")
