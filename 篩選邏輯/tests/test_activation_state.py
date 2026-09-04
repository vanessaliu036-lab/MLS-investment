import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import decision_view
import store


def test_price_trigger_is_not_reported_as_untriggered():
    result = decision_view.activation_view(
        {"volume_confirmed": None, "acceptance_confirmed": None},
        123.5, 119.0,
    )
    assert result["activation_state"] == "PRICE_TRIGGERED"
    assert result["price_triggered"] is True
    assert result["action_code"] == "WAIT_CONFIRMATION"


def test_active_is_independent_from_extension_action():
    result = decision_view.activation_view(
        {"volume_confirmed": True, "acceptance_confirmed": True},
        130.0, 119.0, extension_high=True,
    )
    assert result["activation_state"] == "ACTIVE"
    assert result["trade_state"] == "EXTENDED"
    assert result["action_code"] == "DO_NOT_CHASE"


def test_flow_conflict_blocks_decision_without_becoming_market_reject():
    result = decision_view.activation_view(
        {"aflow_conflict": True, "volume_confirmed": True,
         "acceptance_confirmed": True},
        123.5, 119.0,
    )
    assert result["activation_state"] == "DATA_BLOCKED"
    assert result["trade_state"] == "DATA_BLOCKED"
    assert result["action_code"] == "DATA_BLOCKED"


def test_read_aflow_date_preserves_conflicting_candidates(tmp_path):
    path = str(tmp_path / "flow.db")
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE aflow (
            code TEXT, data_date TEXT, active_buy REAL, active_sell REAL,
            net_active REAL, method TEXT, updated_at TEXT
        )""")
        conn.executemany("INSERT INTO aflow VALUES (?,?,?,?,?,?,?)", [
            ("1815", "2026-08-28", 20000, 3604, 16396, "tick", "2026-08-28T10:00:00"),
            ("1815", "2026-08-28", 0, 5091, -5091, "snapshot", "2026-08-28T10:00:01"),
        ])
    row = store.read_aflow_date("2026-08-28", path)["1815"]
    assert row["aflow_conflict"] is True
    assert row["net_active"] is None
    assert {x["net_active"] for x in row["aflow_candidates"]} == {16396, -5091}
