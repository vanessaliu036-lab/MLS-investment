# -*- coding: utf-8 -*-
"""後驗驗證資料語意與 EOD gate 的回歸測試。"""

import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1] / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(MODULE_DIR))

import review_metrics  # noqa: E402
import db  # noqa: E402
import eod_pipeline  # noqa: E402


def test_v41_hit_metric_uses_canonical_verdicts():
    rows = [
        {"verdict": "A_突破成功", "source": "radar", "close_price": 102},
        {"verdict": "突破延續", "source": "radar", "close_price": 101},
        {"verdict": "B_續強", "source": "radar", "close_price": 101},
        {"verdict": "抗跌成立", "source": "resilient", "close_price": 99},
        {"verdict": "C_未續強", "source": "radar", "close_price": 98},
    ]
    summary = review_metrics.summarize(rows, metric="v4.1_A")
    assert summary["hit"] == 3
    assert summary["total"] == 5
    assert summary["hit_rate"] == 60.0
    assert summary["status"] == "VERIFIED"


def test_empty_or_partial_rows_are_not_zero_hit_days():
    empty = review_metrics.summarize([], metric="v4.1_A")
    assert empty["hit_rate"] is None
    assert empty["status"] == "NO_WATCHLIST"

    partial = review_metrics.summarize(
        [{"verdict": "A_突破成功", "source": "radar", "close_price": None}],
        metric="v4.1_A",
        expected_total=2,
    )
    assert partial["hit_rate"] is None
    assert partial["status"] == "DATA_INCOMPLETE"


def test_db_recomputes_legacy_summary_and_excludes_empty_trend_days(tmp_path):
    original_path = db.DB_PATH
    db.DB_PATH = str(tmp_path / "mls.db")
    try:
        db.init()
        db.save_watchlist("2099-01-02", [
            {"code": "2330", "name": "台積電", "sector": "半導體",
             "reason": "radar", "source": "radar"},
        ])
        db.save_watch_outcome("2099-01-02", [
            {"code": "2330", "name": "台積電", "sector": "半導體",
             "verdict": "A_突破成功", "close_price": 102,
             "change_rate": 2.0},
        ])
        db.write_review("2099-01-02", 1, 0, [])
        db.write_review("2099-01-01", 0, 0, [], status="NO_WATCHLIST")

        summary = db.review_summary("2099-01-02")
        assert summary["watch_hit"] == 1
        assert summary["hit_rate"] == 100.0
        assert summary["data_status"] == "VERIFIED"
        trend = db.recent_hit_rates(30)
        assert [row["trade_date"] for row in trend] == ["2099-01-02"]
    finally:
        db.DB_PATH = original_path


def test_backfill_labels_processes_all_contiguous_pending_dates(tmp_path):
    original_path = db.DB_PATH
    original_today = db.today
    original_universe = eod_pipeline.C.UNIVERSE
    db.DB_PATH = str(tmp_path / "mls.db")
    db.today = lambda: "2099-01-03"
    eod_pipeline.C.UNIVERSE = ("2330",)
    try:
        db.init()
        eod_pipeline._init_tables()
        with db._lock, db._conn() as c:
            c.executemany(
                """INSERT INTO training_samples
                   (trade_date,stock_id,features,close_price,label,label_date)
                   VALUES(?,?,?,?,NULL,NULL)""",
                [("2099-01-01", "2330", "{}", 100),
                 ("2099-01-02", "2330", "{}", 102)],
            )
        filled, last_date = eod_pipeline.backfill_labels(
            [{"code": "2330", "price": 103}])
        assert filled == 2
        assert last_date == "2099-01-02"
        with db._lock, db._conn() as c:
            labels = c.execute(
                "SELECT trade_date,label,label_date FROM training_samples"
                " ORDER BY trade_date").fetchall()
        assert [(r["trade_date"], r["label"], r["label_date"]) for r in labels] == [
            ("2099-01-01", 1, "2099-01-02"),
            ("2099-01-02", 1, "2099-01-03"),
        ]
    finally:
        db.DB_PATH = original_path
        db.today = original_today
        eod_pipeline.C.UNIVERSE = original_universe
