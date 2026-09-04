import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import line_b_research as research


def _ledger(**overrides):
    row = {
        "data_date": "2026-09-03",
        "code": "3006",
        "source": "C1C2_PASS",
        "c1_structure_intact": 1,
        "c2_selling_weak_price_resp": 1,
        "flow_class": "FLOW_FLIP",
        "flow_confirm_magnitude": 200,
        "t1_close": 100.0,
        "t1_ma20": 95.0,
        "t1_prior_high": 102.0,
    }
    row.update(overrides)
    return row


def _snap(slot, price, net_active):
    return {"slot": slot, "price": price, "net_active": net_active}


def test_event_requires_c2_pass_and_confirms_at_second_positive_flow_snapshot():
    event = research.build_event(
        _ledger(),
        [
            _snap("0915", 100.0, -80),
            _snap("0920", 101.0, 120),
            _snap("0925", 102.0, 200),
        ],
    )

    assert event["confirmation_slot"] == "0925"
    assert event["confirmation_price"] == 102.0
    assert event["flow_class"] == "FLOW_FLIP"

    assert research.build_event(
        _ledger(source="INTRADAY_DISCOVERY"),
        [_snap("0915", 100.0, -80), _snap("0920", 101.0, 120), _snap("0925", 102.0, 200)],
    ) is None


def test_forward_intraday_outcomes_use_only_snapshots_after_confirmation_and_net_cost():
    event = {"confirmation_slot": "0920", "confirmation_price": 100.0}
    snapshots = [
        _snap("0915", 99.0, -10),
        _snap("0920", 100.0, 200),
        _snap("0925", 101.0, 100),
        _snap("0930", 99.0, 80),
        _snap("0935", 103.0, 60),
    ]

    outcome = research.intraday_outcomes(event, snapshots, cost_bps=47.1)

    assert outcome["mfe_15m_gross_pct"] == 3.0
    assert outcome["mae_15m_gross_pct"] == -1.0
    assert outcome["mfe_15m_net_pct"] == 2.529
    assert outcome["mae_15m_net_pct"] == -1.471
    assert outcome["execution_lag_bps"] == 100.0


def test_t1_backfill_stays_pending_until_immediate_next_trading_day_exists(tmp_path):
    db_path = str(tmp_path / "research.db")
    research.ensure(db_path)
    research.insert_event(_ledger(), {
        "confirmation_slot": "0925",
        "confirmation_price": 102.0,
        "flow_class": "FLOW_FLIP",
        "flow_confirm_magnitude": 200,
    }, db_path)

    assert research.backfill_t1(db_path, "2026-09-03") == 0

    import sqlite3
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT t1_return_net_pct, t1_matured_date FROM line_b_research"
        ).fetchone()
    assert row == (None, None)


def test_collect_and_backfill_records_event_and_next_day_outcome(tmp_path):
    db_path = str(tmp_path / "research.db")
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE line_b_watch_ledger (
                data_date TEXT, code TEXT, source TEXT,
                c1_structure_intact INTEGER, c2_selling_weak_price_resp INTEGER,
                flow_class TEXT, flow_confirm_magnitude REAL,
                t1_close REAL, t1_ma20 REAL, t1_prior_high REAL
            );
            CREATE TABLE b_snapshot (
                data_date TEXT, code TEXT, slot TEXT, price REAL, net_active REAL
            );
            CREATE TABLE daily_bar (
                data_date TEXT, code TEXT, open REAL, high REAL, low REAL, close REAL
            );
            INSERT INTO line_b_watch_ledger VALUES
                ('2026-09-03','3006','C1C2_PASS',1,1,'FLOW_FLIP',200,100,95,102);
            INSERT INTO b_snapshot VALUES
                ('2026-09-03','3006','0915',99,-50),
                ('2026-09-03','3006','0920',100,100),
                ('2026-09-03','3006','0925',102,200),
                ('2026-09-03','3006','0930',103,180);
            INSERT INTO daily_bar VALUES
                ('2026-09-04','3006',103,106,101,105);
            """
        )

    result = research.collect_and_backfill(
        db_path,
        "2026-09-03",
        {"market_regime": "RISK_ON", "market_regime_raw": "NORMAL",
         "market_breadth_pct": 72.0, "market_context_source": "test"},
    )

    assert result["events_created"] == 1
    assert result["t1_backfilled"] == 1
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT confirmation_slot,t1_return_net_pct,t1_win,market_regime "
            "FROM line_b_research"
        ).fetchone()
    assert row == ("0925", 2.47, 1, "RISK_ON")


def test_collect_is_safe_before_production_source_tables_exist(tmp_path):
    db_path = str(tmp_path / "empty.db")

    result = research.collect_and_backfill(
        db_path, "2026-09-03",
        {"market_regime": "UNKNOWN", "market_regime_raw": "NO_DATA"},
    )

    assert result["events_created"] == 0
    assert result["events_observed"] == 0
    assert result["t1_backfilled"] == 0


def test_line_b_daily_entrypoint_triggers_research_collection(monkeypatch):
    import run_line_b_ledger as runner

    calls = []
    monkeypatch.setattr(runner, "run", lambda: {"written": 1})
    monkeypatch.setattr(runner, "_collect_research", lambda: calls.append(True))

    runner.main()

    assert calls == [True]


def test_summary_reports_matured_t1_and_remaining_payoff_by_market_regime(tmp_path):
    db_path = str(tmp_path / "research.db")
    research.ensure(db_path)
    event = _ledger()
    event.update({"market_regime": "RISK_ON"})
    research.insert_event(event, {
        "confirmation_slot": "0925",
        "confirmation_price": 102.0,
        "flow_class": "FLOW_FLIP",
        "flow_confirm_magnitude": 200,
        "market_regime": "RISK_ON",
    }, db_path)

    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE line_b_research SET t1_matured_date='2026-09-04', "
            "t1_return_net_pct=2.47, t1_win=1, t1_mfe_net_pct=3.45, "
            "t1_mae_net_pct=-1.45, execution_lag_bps=12.0"
        )
        conn.commit()

    result = research.summary(db_path)

    assert result["total"]["n"] == 1
    assert result["total"]["t1_win_rate_pct"] == 100.0
    assert result["by_market_regime"]["RISK_ON"]["avg_t1_mfe_net_pct"] == 3.45
