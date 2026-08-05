"""twstock 收盤資料轉接層；FinMind 僅作最後備援。"""
from __future__ import annotations

import datetime as _dt


def fetch(code: str, end_date: _dt.date, months: int = 5) -> list[dict]:
    try:
        import twstock
    except Exception:
        return []
    out: dict[str, dict] = {}
    cursor = end_date.replace(day=1)
    stock = twstock.Stock(str(code))
    for _ in range(months):
        try:
            rows = stock.fetch_from(cursor.year, cursor.month)
        except Exception:
            rows = []
        for row in rows or []:
            d = getattr(row, "date", None)
            close = getattr(row, "close", None)
            if isinstance(d, _dt.datetime):
                d = d.date()
            if not isinstance(d, _dt.date) or d > end_date or close is None:
                continue
            out[d.isoformat()] = {
                "date": d.isoformat(), "open": getattr(row, "open", None),
                "max": getattr(row, "high", None), "min": getattr(row, "low", None),
                "close": close, "Trading_Volume": getattr(row, "capacity", None),
            }
        cursor = (cursor - _dt.timedelta(days=1)).replace(day=1)
    return [out[k] for k in sorted(out)]
