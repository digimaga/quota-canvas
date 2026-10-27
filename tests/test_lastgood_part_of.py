"""last-good.json の part_of (内訳窓の親窓 key) の保存・読み込みのテスト。

part_of 付きの窓を往復できることと、part_of キーが無い古い last-good.json
(part_of=None として読む) を壊さずに読めること。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from quotacanvas.lastgood import LastGood
from quotacanvas.models import ProviderUsage, Window

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)
LAST_GOOD = "last-good.json"


def _grok_usage():
    main = Window("grok", "weekly", "週間", 22.0,
                  NOW + timedelta(days=5), timedelta(days=7))
    parts = [Window("grok", "weekly_build", "週間 Build", 20.0,
                    main.resets_at, main.length, part_of="weekly"),
             Window("grok", "weekly_tasks", "週間 Tasks", 2.0,
                    main.resets_at, main.length, part_of="weekly")]
    return ProviderUsage("grok", "Grok", [main] + parts, None, NOW)


def test_part_of_roundtrip(tmp_path):
    usage = _grok_usage()
    LastGood(tmp_path / LAST_GOOD).set(usage)
    loaded = LastGood(tmp_path / LAST_GOOD).data["grok"]
    assert loaded == usage                        # part_of も含めて往復する

    on_disk = json.loads((tmp_path / LAST_GOOD).read_text(encoding="utf-8"))
    assert [w["part_of"] for w in on_disk["grok"]["windows"]] == \
        [None, "weekly", "weekly"]


def test_part_of_missing_key_reads_as_none(tmp_path):
    """part_of キーが無い古い last-good.json は part_of=None として読む。"""
    old = {"grok": {"name": "Grok", "error": None,
                    "fetched_at": NOW.isoformat(), "retry_after": None,
                    "reset_credits": [],
                    "windows": [
                        {"key": "weekly", "label": "週間",
                         "used_percent": 22.0,
                         "resets_at": (NOW + timedelta(days=5)).isoformat(),
                         "length": 604800.0},
                        {"key": "weekly_build", "label": "週間 Build",
                         "used_percent": 20.0,
                         "resets_at": (NOW + timedelta(days=5)).isoformat(),
                         "length": 604800.0, "part_of": "weekly"},
                    ]}}
    (tmp_path / LAST_GOOD).write_text(json.dumps(old), encoding="utf-8")
    loaded = LastGood(tmp_path / LAST_GOOD).data["grok"]
    assert [w.part_of for w in loaded.windows] == [None, "weekly"]
    assert loaded.windows[0].id == "grok:weekly"
    assert loaded.windows[1].used_percent == 20.0


def test_non_dict_window_entry_skips_only_that_window(tmp_path):
    """窓要素が dict でない (文字列など) ときも、読み込み全体は落ちずに
    その窓だけを飛ばす。"""
    old = {"grok": {"name": "Grok", "error": None,
                    "fetched_at": NOW.isoformat(), "retry_after": None,
                    "reset_credits": [],
                    "windows": [
                        "weekly",                       # dict でない
                        {"key": "weekly", "label": "週間",
                         "used_percent": 22.0,
                         "resets_at": (NOW + timedelta(days=5)).isoformat(),
                         "length": 604800.0},
                        {"key": "weekly_build", "label": "週間 Build",
                         "used_percent": 20.0,
                         "resets_at": (NOW + timedelta(days=5)).isoformat(),
                         "length": 604800.0, "part_of": "weekly"},
                    ]}}
    (tmp_path / LAST_GOOD).write_text(json.dumps(old), encoding="utf-8")
    loaded = LastGood(tmp_path / LAST_GOOD).data["grok"]
    assert [w.key for w in loaded.windows] == ["weekly", "weekly_build"]
