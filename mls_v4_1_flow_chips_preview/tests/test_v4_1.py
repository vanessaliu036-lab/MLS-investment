import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mls_v4_1_flow_chips.analyzer import AnalysisInput, analyze
from mls_v4_1_flow_chips.history import scenario_stats
from mls_v4_1_flow_chips.preview_server import create_app
from mls_v4_1_flow_chips.repository import (
    aflow_positive_two_samples,
    apply_schema,
    chip_4d_summary,
    consecutive_flow_ticks,
    current_top_rows,
)
from mls_v4_1_flow_chips.rules import (
    classify_volume_quality,
    compute_clv,
    price_acceptance,
    price_freshness,
)
from mls_v4_1_flow_chips.service import build_top10
from mls_v4_1_flow_chips.source_bridge import RequiredColumnsError, read_mapped_rows

SCHEMA = Path(__file__).parents[1] / "mls_v4_1_flow_chips" / "schema.sql"


def memory_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_schema(c, SCHEMA)
    return c


def base(**overrides):
    data = dict(
        symbol="3374", name="精材", trade_date="2026-08-28",
        price_data_date="2026-08-28", chip_data_date="2026-08-27",
        flow_data_time="13:25:00", snapshot_time="13:25:01",
        net_flow_amount=-36_000_000, flow_threshold=10_000_000,
        flow_consecutive_ticks=3, price_change_pct=-1.0,
        high=110, low=100, close=109, prev_close=100, vwap=106,
        volume=1800, ma5_volume=1000, net_active=1.0,
        aflow_positive_2_samples=True, foreign_net_4d=8000,
        volume_4d=100000, big_holder_trend=0.2,
        trigger_failed=True, trigger_passed=False,
        market_regime="RISK_ON", rescue_rule_approved=False,
    )
    data.update(overrides)
    return AnalysisInput(**data)


def insert_snap(c, symbol, ts, flow, aflow=1, close=100):
    c.execute("""INSERT INTO intraday_snapshot(
        trade_date,symbol,stock_name,ts,open,high,low,close,prev_close,
        volume,ma5_volume,vwap,a_flow,net_active,bid_ask_ratio,net_flow_amount,
        turnover_ratio,price_change_pct,price_data_date,flow_data_time,as_of)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("2026-09-01",symbol,symbol,ts,99,102,98,close,99,1000,1000,99.5,
         aflow,1,1.2,flow,0.1,1.0,"2026-09-01",ts,f"2026-09-01T{ts}+08:00"))


def test_freshness_and_clv_rules():
    assert price_freshness("2026-08-27", "2026-08-28") == "STALE"
    value, confidence = compute_clv(110, 100, 109, 100)
    assert round(value, 2) == 0.90 and confidence == "VALID"
    value, confidence = compute_clv(101, 99, 100.8, 100)
    assert confidence == "LOW_CONFIDENCE"
    assert price_acceptance(value, confidence, close=100.8, vwap=100.4, prev_close=100)


def test_volume_quality_matrix():
    assert classify_volume_quality(.90, "VALID", .6) == "SHAKEOUT"
    assert classify_volume_quality(.90, "VALID", 1.8) == "HEAVY_ABSORPTION"
    assert classify_volume_quality(.35, "VALID", .6) == "NATURAL_DECAY"
    assert classify_volume_quality(.35, "VALID", 1.8) == "FLOW_PRICE_DIVERGENCE"


def test_stale_blocks_action_and_rescue_is_observation_only_before_validation():
    assert analyze(base(price_data_date="2026-08-27")).action == "OBSERVE_ONLY"
    rescue = analyze(base())
    assert rescue.state == "FALSE_FAIL_RESCUE_HIGH"
    assert rescue.action == "OBSERVE_ONLY"


def test_divergence_is_true_fail():
    r = analyze(base(close=103.5, high=110, low=100, vwap=106, volume=1800, ma5_volume=1000))
    assert r.volume_quality == "FLOW_PRICE_DIVERGENCE"
    assert r.state == "TRUE_FAIL" and r.action == "EXCLUDE"


def test_four_quadrant_actions():
    good = analyze(base(net_flow_amount=35_000_000, price_change_pct=2, trigger_failed=False, trigger_passed=True))
    assert good.action == "CONSIDER_ENTRY"
    weak = analyze(base(net_flow_amount=35_000_000, foreign_net_4d=-2000, trigger_failed=False, trigger_passed=True))
    assert weak.action == "WAIT"


def test_repository_latest_ticks_and_chip_summary():
    c = memory_db()
    insert_snap(c, "A", "09:05:00", 12, 1)
    insert_snap(c, "A", "09:10:00", 14, 2)
    insert_snap(c, "A", "09:15:00", 16, 3)
    insert_snap(c, "B", "09:15:00", 13, 3)
    rows = current_top_rows(c, "2026-09-01", "inflow", 10)
    assert rows[0]["symbol"] == "A"
    assert consecutive_flow_ticks(c, "A", "2026-09-01", 10) == 3
    assert aflow_positive_two_samples(c, "A", "2026-09-01")
    for d, f in [("2026-08-25",100),("2026-08-26",200),("2026-08-27",-50),("2026-08-28",300)]:
        c.execute("INSERT INTO chip_daily VALUES(?,?,?,?,?,?,?,?)",(d,"A",f,f,1000,.2,d,d+"T16:00:00+08:00"))
    s = chip_4d_summary(c, "A", "2026-09-01")
    assert s["foreign_net_4d"] == 550 and s["volume_4d"] == 4000


def test_history_hides_rate_below_twenty():
    c = memory_db()
    for _ in range(10):
        c.execute("INSERT INTO decision_history(scenario,market_regime,success,next_day_up,plus3,plus5,mfe,mae,baseline_up_rate) VALUES(?,?,?,?,?,?,?,?,?)",
                  ("X","RISK_ON",1,1,0,0,2,-1,.5))
    s = scenario_stats(c, "X")
    assert s["n"] == 10 and s["display_rate"] is None


def test_source_bridge_requires_explicit_columns():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE flow(symbol TEXT, net_flow_amount REAL)")
    with pytest.raises(RequiredColumnsError):
        read_mapped_rows(c, "SELECT * FROM flow", required={"symbol","net_flow_amount","trade_date"})


def test_standalone_server(tmp_path):
    app = create_app(tmp_path / "preview.db")
    client = TestClient(app)
    assert client.get("/api/flow-chips/health").json()["mode"] == "isolated-preview"
    assert "Flow × Chips" in client.get("/").text
