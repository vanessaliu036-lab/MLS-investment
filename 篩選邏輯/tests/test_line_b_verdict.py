import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import line_b_verdict as verdict
import line_b_ledger_view as view


def test_cumulative_confirmed_rate_uses_all_c2_aflow_confirmed_rows(tmp_path):
    db_path = str(tmp_path / "verdict.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE line_b_watch_ledger ("
            "data_date TEXT, code TEXT, source TEXT, "
            "c2_selling_weak_price_resp INTEGER, flow_class TEXT, "
            "watch_mode_activated INTEGER)"
        )
        rows = [
            ("2026-08-26", "old", "C1C2_PASS", 1, "OPEN_POSITIVE", 0),
            ("2026-08-27", "a", "C1C2_PASS", 1, "OPEN_POSITIVE", 1),
            ("2026-08-28", "b", "C1C2_PASS", 1, "FLOW_FLIP", 0),
            ("2026-08-31", "c", "C1C2_PASS", 1, "OPEN_POSITIVE", 1),
            ("2026-09-01", "d", "C1C2_PASS", 1, "FLOW_FLIP", 1),
            ("2026-09-02", "e", "C1C2_PASS", 1, "OPEN_POSITIVE", 1),
            ("2026-09-03", "f", "C1C2_PASS", 1, "FLOW_FLIP", 0),
            ("2026-09-03", "not-c2", "C1C2_PASS", 0, "OPEN_POSITIVE", 1),
            ("2026-09-03", "discovery", "INTRADAY_DISCOVERY", 1, "OPEN_POSITIVE", 1),
            ("2026-09-03", "no-flow", "C1C2_PASS", 1, "NO_FLIP", 1),
        ]
        conn.executemany("INSERT INTO line_b_watch_ledger VALUES (?,?,?,?,?,?)", rows)

    result = verdict.cumulative_confirmed_rates(db_path)

    assert result["data_dates"] == [
        "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31",
        "2026-09-01", "2026-09-02", "2026-09-03",
    ]
    assert result["n"] == 7
    assert result["hit_count"] == 4
    assert result["no_hit_count"] == 3
    assert result["hit_rate_pct"] == 57.1


def test_cumulative_confirmed_rates_are_safe_without_ledger_table(tmp_path):
    result = verdict.cumulative_confirmed_rates(str(tmp_path / "empty.db"))

    assert result["n_days"] == 0
    assert result["hit_rate_pct"] is None


def test_page_labels_show_cumulative_sample_not_frozen_historical_rate(tmp_path):
    db_path = str(tmp_path / "labels.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE line_b_watch_ledger ("
            "data_date TEXT, code TEXT, source TEXT, "
            "c2_selling_weak_price_resp INTEGER, flow_class TEXT, "
            "watch_mode_activated INTEGER)"
        )
        conn.executemany(
            "INSERT INTO line_b_watch_ledger VALUES (?,?,?,?,?,?)",
            [("2026-09-01", "a", "C1C2_PASS", 1, "OPEN_POSITIVE", 1),
             ("2026-09-02", "b", "C1C2_PASS", 1, "FLOW_FLIP", 0)],
        )

    labels = view._page_labels(db_path)

    assert labels["flow_confirmed_rate"] == "50.0%"
    assert labels["flow_confirmed_label"] == "A-flow 確認後累積命中率"
    assert "累積 2 個交易日" in labels["flow_confirmed_sample_note"]
