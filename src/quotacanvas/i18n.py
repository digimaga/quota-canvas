"""表示文字列の多言語化 (追加依存なし)。

文言は ``locales/<言語>/<領域>.toml`` に置く。キーは ``<領域>.<TOML のキー>``
(例: ``locales/ja/render.toml`` の ``[badge] tight = "使い切り"`` は
``t("render.badge.tight")``)。値の差し込みは ``{pct}`` のような名前付きで、
``t("render.advice.slow_down", pct=12)`` のように渡す (語順が言語ごとに違うため)。

- set_language("auto" | "ja" | "en"): 表示言語を切り替える。auto は macOS の言語設定
- t(key, **kw): 現在の言語の文言。無ければ日本語、それも無ければキーそのもの
- translations(key): 全言語での訳文の集合 (焼き付いた文字列との比較用)
- 領域ファイルは並行して増やせる (別々のファイルなので文言の追加がぶつからない)
"""

from __future__ import annotations

import logging
import subprocess
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Dict, Set

log = logging.getLogger("quotacanvas")

LANGUAGES = ("ja", "en")
LANGUAGE_CHOICES = ("auto",) + LANGUAGES
FALLBACK = "ja"
LOCALES_DIR = Path(__file__).parent / "locales"

_current = FALLBACK


def _flatten(prefix: str, table: dict, out: Dict[str, str]) -> None:
    for key, value in table.items():
        full = "%s.%s" % (prefix, key)
        if isinstance(value, dict):
            _flatten(full, value, out)
        else:
            out[full] = str(value)


@lru_cache(maxsize=None)
def catalog(lang: str) -> Dict[str, str]:
    """``lang`` の全文言をキー -> 文字列の辞書で返す (ファイルは 1 回だけ読む)。"""
    out: Dict[str, str] = {}
    for path in sorted((LOCALES_DIR / lang).glob("*.toml")):
        with path.open("rb") as fh:
            _flatten(path.stem, tomllib.load(fh), out)
    return out


def system_language() -> str:
    """macOS の優先言語の先頭が日本語なら ja、それ以外は en。"""
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return FALLBACK
    for line in out.splitlines():
        code = line.strip().strip('(),"').strip()
        if code:
            return "ja" if code.lower().startswith("ja") else "en"
    return FALLBACK


def resolve(choice: str) -> str:
    """設定値 ("auto" / "ja" / "en") を実際の言語コードにする。"""
    if choice in LANGUAGES:
        return choice
    return system_language()


def set_language(choice: str) -> str:
    """表示言語を切り替え、決まった言語コードを返す。"""
    global _current
    _current = resolve(choice)
    return _current


def current_language() -> str:
    return _current


def translations(key: str) -> Set[str]:
    """全言語での ``key`` の訳文の集合。

    取得時の言語で焼き付いた文字列 (ProviderUsage.error など) を現在の
    言語の訳文と比べるときに使う (== だと、言語を切り替えた直後に一致
    しなくなる)。
    """
    out: Set[str] = set()
    for lang in LANGUAGES:
        text = catalog(lang).get(key)
        if text is not None:
            out.add(text)
    return out


def t(key: str, /, **kw: object) -> str:
    text = catalog(_current).get(key)
    if text is None:
        text = catalog(FALLBACK).get(key)
    if text is None:
        log.warning("i18n: 文言が見つかりません: %s", key)
        return key
    return text.format(**kw) if kw else text
