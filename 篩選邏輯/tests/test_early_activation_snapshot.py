"""Independent Early Activation snapshot ledger."""
import ast
import datetime as dt
import inspect
import os
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import early_activation_score as eas
import early_activation_snapshot as snap


def db_path():
    return os.path.join(tempfile.mkdtemp(), "early.db")


def row(code="2481", setup=eas.NEW_TURN, close=100.0, volume_ratio=0.7):
    return {
        "code": code,
        "close": close,
        "foreign_days": 2,
        "foreign_net": 693,
        "ma5_distance_pct": 0.88,
        "volume_ratio": volume_ratio,
        "sector_regime": "NEUTRAL",
        "sector_ret_median": 0.6,
        "sector_breadth": 50.0,
        "early_activation": {
            "setup_type": setup,
            "sector_context": eas.TURNING_POSITIVE,
            "evidence_status": eas.DISCOVERY_ONLY,
            "reasons": ["FRESH_FOREIGN_BUY_STREAK"],
            "rule_version": eas.RULE_VERSION,
        },
        "early_history": [{"data_date": "2026-08-24", "foreign_days": 1}],
    }


def test_module_and_table_are_independent_from_opportunity_pipeline():
    tree = ast.parse(inspect.getsource(snap))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert not any("opportunity" in ast.unparse(node).lower() for node in imports)
    db = db_path()
    snap.ensure(db)
    tables = {r[0] for r in sqlite3.connect(db).execute(
        "select name from sqlite_master where type='table'")}
    assert snap.TABLE in tables
    assert "opportunity_snapshot" not in tables


def test_snapshot_stores_discovery_facts_without_scores_or_confidence():
    db = db_path()
    assert snap.write_snapshot(dt.date(2026, 8, 25), [row()], db) == 1
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    stored = dict(conn.execute(f"select * from {snap.TABLE}").fetchone())
    assert stored["evidence_status"] == eas.DISCOVERY_ONLY
    assert stored["setup_type"] == eas.NEW_TURN
    columns = set(stored)
    assert not any(word in col for col in columns
                   for word in ("score", "probability", "confidence"))


def test_same_day_identical_write_is_noop_but_changed_fact_is_refused():
    db = db_path()
    snap.write_snapshot(dt.date(2026, 8, 25), [row()], db)
    assert snap.write_snapshot(dt.date(2026, 8, 25), [row()], db) == 0
    try:
        snap.write_snapshot(dt.date(2026, 8, 25), [row(volume_ratio=0.9)], db)
        assert False, "changed T0 facts must not overwrite the discovery snapshot"
    except snap.SnapshotMutationRefused:
        pass


def test_t1_backfill_uses_t0_close_to_t1_close_and_sets_hit():
    db = db_path()
    snap.write_snapshot(dt.date(2026, 8, 25), [row(close=100.0)], db)
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE daily_bar (code TEXT, data_date TEXT, close REAL);")
    conn.execute("INSERT INTO daily_bar VALUES (?,?,?)", ("2481", "2026-08-25", 100.0))
    conn.execute("INSERT INTO daily_bar VALUES (?,?,?)", ("2481", "2026-08-26", 104.0))
    conn.commit()
    assert snap.backfill_t1(db) == 1
    stored = conn.execute(
        f"SELECT t1_close,t1_return_pct,hit_plus_3 FROM {snap.TABLE}").fetchone()
    assert stored == (104.0, 4.0, 1)


def test_summary_uses_setup_and_no_setup_rows_from_own_table():
    db = db_path()
    rows = [row("2481", eas.NEW_TURN), row("1111", None)]
    snap.write_snapshot(dt.date(2026, 8, 25), rows, db)
    conn = sqlite3.connect(db)
    conn.execute(f"UPDATE {snap.TABLE} SET t1_return_pct=8.0,hit_plus_3=1 WHERE code='2481'")
    conn.execute(f"UPDATE {snap.TABLE} SET t1_return_pct=-1.0,hit_plus_3=0 WHERE code='1111'")
    conn.commit()
    report = snap.research_summary(db)
    cell = report["by_setup_context"][0]
    assert cell["metrics"]["n"] == 1
    assert cell["matched_baseline"]["n"] == 1
    assert report["evidence_status"] == eas.DISCOVERY_ONLY

