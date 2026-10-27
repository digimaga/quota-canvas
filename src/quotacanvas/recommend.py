"""今使う先 (F1): サービスをまたいで「今どれを使うのが得か」を 1 行で出す。

純関数 recommend(usages, history, now, tight, on_track) が、取得エラーのない
サービスの独立窓 (part_of が None) それぞれについて「使えるか」と「急ぐ度合い
(score)」を出し、一番 score の高い使えるサービスを推奨する。

- 使えない (usable = false, 推奨しない):
  - どれかの独立窓が EXHAUSTED / OVER、または残り 5% 未満
  - どれかの独立窓の resets_at が過ぎている (MIN_HOURS の床で score が跳ねる
    ので「リセット待ち」として外す)
  - 取得失敗中で last-good を表示している (stale_error あり。データが古いので
    「取得失敗中」として外す)
- score: 代表窓 (そのサービスで最もリセットが遠い独立窓) について、リセット時に
  使われずに消える見込み量 max(0, 100 - projected_end_percent) (FRESH/UNKNOWN で
  予測が無ければ remaining) に代表窓の長さ (時間) を掛け、リセットまでの時間
  (時間単位、最小 1) で割る (= リセットまでに失う「窓の時間ぶんの価値」の速度。
  窓の長さで重み付けするので、短い 5 時間枠が週間より常に勝つことはない)。
  同じサービスの短い窓 (1 日未満) の残りが 20% 未満なら score は半分。
- リセット権 (Codex) が 24 時間以内に失効するなら score を 1.5 倍にして理由に添える

文言は locales/<ja|en>/recommend.toml。時刻の書式は render のものを流用する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from .i18n import t
from .config import sleep_for, wants_bedtime  # pacing.sleep → provider ごとの適用判定
from .models import Pace, PaceStatus, ProviderUsage, Window
from .pacing import compute_pace
from .render import _fmt_num, _fmt_span

__all__ = ["Recommendation", "recommend"]

# score / 使える判定のしきい値
MIN_USABLE_REMAINING = 5.0      # 残りがこれ未満の独立窓があれば使えない
SHORT_WINDOW = timedelta(days=1)      # 「短い窓」= 長さが 1 日未満 (5時間など)
SHORT_WINDOW_LOW = 20.0         # 短い窓の残りがこれ未満なら score を半分
CREDIT_SOON = timedelta(hours=24)     # リセット権がこれ以内に失効したら…
CREDIT_BOOST = 1.5                    # …score をこの倍数にする
MIN_HOURS = 1.0                 # リセットまでの時間 / 窓の長さの下限 (時間単位)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class Recommendation:
    """今使うべきサービス 1 件ぶん (status.json の "recommendation" と同じ形)。"""

    provider: str
    name: str
    window_id: str
    text: str                      # 「Codex を使う · 週の残り 62% · リセットまで 1日3時間」
    ranking: List[Dict[str, Any]] = field(default_factory=list)

    def payload(self) -> Dict[str, Any]:
        """status.json / `pick --json` に入れる dict。"""
        return {
            "provider": self.provider,
            "name": self.name,
            "window_id": self.window_id,
            "text": self.text,
            "ranking": [dict(entry) for entry in self.ranking],
        }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone().isoformat(timespec="seconds") if dt is not None else None


def _entry(provider: str, name: str, window: Window, usable: bool, score: float,
           reason: str) -> Dict[str, Any]:
    return {
        "provider": provider,
        "name": name,
        "window_id": window.id,
        "usable": usable,
        "score": round(score, 3),
        "remaining": round(window.remaining_percent, 1),
        "resets_at": _iso(window.resets_at),
        "reason": reason,
    }


def _waste(pace: Pace) -> float:
    """リセットまでに使われずに消える見込み量 (%): 予測があれば
    max(0, 100 - projected_end_percent)、無ければ (FRESH/UNKNOWN) remaining。"""
    if pace.projected_end_percent is not None:
        return max(0.0, 100.0 - pace.projected_end_percent)
    return pace.window.remaining_percent


def _hours(span: timedelta) -> float:
    """時間単位にして MIN_HOURS 以上に丸める。"""
    return max(MIN_HOURS, span.total_seconds() / 3600.0)


def _hours_until(window: Window, now: datetime) -> float:
    """リセットまでの時間 (時間単位、最小 1)。リセット時刻が不明なら窓の長さ。"""
    if window.resets_at is not None:
        return _hours(window.resets_at - now)
    return _hours(window.length)


def _reason(window: Window, now: datetime,
            credit_span: Optional[timedelta]) -> str:
    """「週の残り 62% · リセットまで 1日3時間 (· リセット権が 18時間で失効)」。"""
    parts = [t("recommend.parts.remaining", label=window.label or window.key,
               pct=_fmt_num(window.remaining_percent))]
    if window.resets_at is not None:
        parts.append(t("recommend.parts.reset_in",
                       span=_fmt_span(window.resets_at - now)))
    if credit_span is not None:
        parts.append(t("recommend.parts.credit_soon", span=_fmt_span(credit_span)))
    return " · ".join(parts)


def recommend(usages: Sequence[ProviderUsage], history, now: datetime,
              tight: float = 92.0, on_track: float = 70.0,
              sleep=None) -> Optional[Recommendation]:
    """一番「使わないと消える」量の多い使えるサービスを 1 つ返す (無ければ None)。

    usages の各サービスについて独立窓 (part_of が None) だけを見る。pace は
    history のサンプルからここで計算し直す。`sleep` (config pacing.sleep)
    があれば、skip に入っていないサービスのペース判定を「起きている時間」で
    行う (compute_pace に (start, end) のローカル時刻を渡す)。advice =
    "bedtime" なら Pace.bedtime_at も載せる (recommend 自体は使わない)。
    """
    candidates: List[Dict[str, Any]] = []
    for usage in usages:
        if usage.error:
            continue
        windows = [w for w in usage.windows if w.part_of is None]
        if not windows:
            continue
        paces = {w.id: compute_pace(w, history.samples(w.id), now,
                                    tight=tight, on_track=on_track,
                                    sleep=sleep_for(sleep, usage.provider),
                                    bedtime=wants_bedtime(sleep, usage.provider))
                 for w in windows}
        # 代表窓: 最もリセットが遠い独立窓 (週間など)
        rep = max(windows, key=lambda w: (w.resets_at or _EPOCH))
        score = _waste(paces[rep.id]) * _hours(rep.length) / _hours_until(rep, now)
        # 短い窓 (5時間など) の残りが少ないときは、すぐその窓に移るべきなので半分
        if any(w.length < SHORT_WINDOW and w.remaining_percent < SHORT_WINDOW_LOW
               for w in windows):
            score *= 0.5
        credit_span = _soon_credit(usage, now)
        if credit_span is not None:
            score *= CREDIT_BOOST
        # 使えないケース: 窓が OVER/枯渇・残り 5% 未満、代表窓の resets_at が過ぎている
        # (MIN_HOURS の床で score が跳ねるので候補から外す)、取得失敗中。
        # 短い窓がリセット待ちなだけなら、代表窓の score は正しいので外さない。
        waiting = rep.resets_at is not None and rep.resets_at <= now
        if usage.stale_error is not None:
            # last-good を表示中: データが古いので推奨できない
            reason = t("recommend.reason.stale")
        elif waiting:
            reason = t("recommend.reason.waiting_reset")
        else:
            reason = _reason(rep, now, credit_span)
        usable = (
            usage.stale_error is None
            and not waiting
            and all(paces[w.id].status not in (PaceStatus.EXHAUSTED, PaceStatus.OVER)
                    and w.remaining_percent >= MIN_USABLE_REMAINING
                    for w in windows)
        )
        candidates.append(_entry(usage.provider, usage.name, rep, usable, score,
                                 reason))
    if not candidates:
        return None
    candidates.sort(key=lambda e: (not e["usable"], -e["score"]))
    best = candidates[0]
    if not best["usable"]:
        return None
    return Recommendation(
        provider=best["provider"],
        name=best["name"],
        window_id=best["window_id"],
        text=t("recommend.main.use", name=best["name"], reason=best["reason"]),
        ranking=candidates,
    )


def _soon_credit(usage: ProviderUsage, now: datetime) -> Optional[timedelta]:
    """24 時間以内に失効するリセット権があれば、最も早い失効までの時間。"""
    spans = [c.expires_at - now for c in (usage.reset_credits or [])
             if c.expires_at is not None and timedelta(0) <= c.expires_at - now <= CREDIT_SOON]
    return min(spans) if spans else None
