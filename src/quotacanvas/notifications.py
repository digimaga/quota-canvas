"""Pure notification candidate selection, shared by desktop clients."""
from datetime import datetime
from typing import Any


def _string(value: Any):
    return value if isinstance(value, str) else None


def _date(value: Any):
    value = _string(value)
    if value is None or 'T' not in value:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _cards(status):
    cards = status.get('cards') if isinstance(status, dict) else None
    return cards if isinstance(cards, list) else []


def _key(card):
    ident = _string(card.get('id'))
    if ident is not None:
        return ident
    provider, label = _string(card.get("provider")), _string(card.get("label"))
    return f"{provider if provider is not None else '?'}|{label if label is not None else '?'}"


def _same_reset(first, second):
    if first is None or second is None:
        return first is None and second is None
    return abs((first - second).total_seconds()) <= 60


def plan(previous: dict | None, current: dict, now: datetime | float, notified: set[str]) -> list[dict]:
    """Return candidates without I/O, persistence, or OS notification delivery."""
    result = []
    now_seconds = now.timestamp() if isinstance(now, datetime) else now
    cards = [card for card in _cards(current) if isinstance(card, dict)]
    if previous is not None:
        previous_cards = {_key(c): c for c in _cards(previous) if isinstance(c, dict)}
        for card in cards:
            ident = _key(card)
            old = previous_cards.get(ident, {})
            reset = _date(card.get('resets_at'))
            old_reset = _date(old.get('resets_at'))
            period = str(int(reset.timestamp() / 300)) if reset else '-'
            status = _string(card.get('status'))
            if status in ('over', 'exhausted'):
                key = f'status|{ident}|{period}|{status}'
                if (status != _string(old.get('status')) or not _same_reset(old_reset, reset)) and key not in notified:
                    result.append({'key': key, 'kind': 'status', 'card': card})
            if reset is not None and old_reset is not None and not _same_reset(reset, old_reset):
                key = f'reset|{ident}|{period}'
                if key not in notified:
                    result.append({'key': key, 'kind': 'reset', 'card': card})
            if _string(card.get('burst_exhausts_at')) is not None and _string(old.get('burst_exhausts_at')) is None and _date(card.get('burst_exhausts_at')) is not None:
                key = f'burst|{ident}|{period}'
                if key not in notified:
                    result.append({'key': key, 'kind': 'burst', 'card': card})
    credits = current.get('reset_credits')
    # Swift decodes the whole reset_credits field as one optional dictionary.
    if isinstance(credits, dict) and all(isinstance(items, list) and
                                         all(isinstance(item, dict) for item in items)
                                         for items in credits.values()):
        for provider, items in sorted(credits.items()):
            for credit in items:
                raw = _string(credit.get('expires_at'))
                expires = _date(raw)
                if expires is None or not 0 < expires.timestamp() - now_seconds <= 86400:
                    continue
                key = f"credit|{provider}|{_string(credit.get('title')) or ''}|{raw}"
                if key not in notified:
                    name = next((_string(c.get('name')) for c in cards if _string(c.get('provider')) == provider), None)
                    result.append({'key': key, 'kind': 'credit', 'provider': provider,
                                   'name': name if name is not None else provider, 'expires_at': raw})
    return result
