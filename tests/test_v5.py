# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.classify import classify_flat  # noqa: E402
from app.eod_stamp import (load_eod, list_trade_dates, load_stock_history,
                           run_eod_stamp, stock_trend_summary)  # noqa: E402
from app.intraday_filter import StockSnap  # noqa: E402


def snap(**updates):
    data = dict(code="0000", track="attack", price=100, change_rate=-3,
                aflow=500, total_volume=2000, ma20=None)
    data.update(updates)
    return StockSnap(**data)


def test_classify_flat_puts_actionable_first():
    rows = classify_flat([
        snap(code="exclude", change_rate=-9.72, aflow=9999),
        snap(code="watch", change_rate=2, aflow=-5, ma20=None),
        snap(code="action", change_rate=-3, aflow=3000, total_volume=5000),
    ], regime="防守盤")
    assert rows[0]["group"] == "可操作"
    assert rows[-1]["group"] == "排除"


def test_eod_stamp_is_daily_upsert_and_marks_extreme_unreliable():
    db = tempfile.mktemp(suffix=".db")
    try:
        run_eod_stamp(db, [snap(code="x", change_rate=-9.72)], 30,
                      trade_date="2026-07-20")
        run_eod_stamp(db, [snap(code="x", change_rate=-8.0, aflow=900)], 30,
                      trade_date="2026-07-20")
        run_eod_stamp(db, [snap(code="x", change_rate=-8.0)], 30,
                      trade_date="2026-07-21")
        rows = load_eod(db, "2026-07-20")
        assert len(rows) == 1
        assert rows[0]["aflow"] == 900
        assert len(load_eod(db, "2026-07-21")) == 1
        assert list_trade_dates(db) == ["2026-07-21", "2026-07-20"]
        assert len(load_stock_history(db, "x", 5)) == 2
        assert stock_trend_summary(db, "x")["trend"] == "持續可操作"
    finally:
        if os.path.exists(db):
            os.remove(db)
