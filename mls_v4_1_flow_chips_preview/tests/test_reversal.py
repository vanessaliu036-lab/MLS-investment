import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from mls_v4_1_flow_chips.preview_server import create_app
from mls_v4_1_flow_chips.repository import apply_schema
from mls_v4_1_flow_chips.reversal import classify_reversal
from mls_v4_1_flow_chips.service import build_reversal_day1

SCHEMA = Path(__file__).parents[1] / "mls_v4_1_flow_chips" / "schema.sql"


def make_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_schema(c, SCHEMA)
    c.execute("INSERT INTO flow_threshold_config VALUES(?,?,?,?)", ("default", 100, 2, "test"))
    c.execute("INSERT INTO market_regime_daily VALUES(?,?,?,?,?)", ("2026-09-01", "RISK_ON", .5, .5, "2026-09-01T09:00:00+08:00"))
    return c


def seed_chips(c, symbol, inst=-1000, foreign=-500, days=20):
    dates = [f"2026-08-{d:02d}" for d in range(9, 29)][-days:]
    for d in dates:
        c.execute(
            "INSERT INTO chip_daily VALUES(?,?,?,?,?,?,?,?)",
            (d, symbol, foreign, inst, 10000, 0.1, d, d + "T16:00:00+08:00"),
        )


def seed_snap(c, symbol, name, ts, close, aflow, change, vwap, volume=100000):
    c.execute(
        """INSERT INTO intraday_snapshot(
        trade_date,symbol,stock_name,ts,open,high,low,close,prev_close,volume,ma5_volume,
        vwap,a_flow,net_active,bid_ask_ratio,net_flow_amount,turnover_ratio,price_change_pct,
        price_data_date,flow_data_time,as_of) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("2026-09-01", symbol, name, ts, close-2, close+1, close-3, close, close/(1+change/100),
         volume, 200000, vwap, aflow, 100, 1.2, aflow*1000, .1, change,
         "2026-09-01", ts, "2026-09-01T"+ts+"+08:00"),
    )


def test_classify_reversal_requires_r1_to_r5_for_priority():
    assert classify_reversal(
        prior_outflow=True, extreme_outflow=True, price_reversal=True,
        aflow_flip=True, aflow_persistence=True, price_confirmation=True,
        above_vwap=True, stale_price=False,
    ) == "REVERSAL_PRIORITY"


def test_classify_reversal_early_signal_does_not_wait_for_persistence():
    assert classify_reversal(
        prior_outflow=True, extreme_outflow=True, price_reversal=True,
        aflow_flip=True, aflow_persistence=False, price_confirmation=False,
        above_vwap=True, stale_price=False,
    ) == "REVERSAL_DAY1_EARLY"


def test_8150_like_case_has_persistent_aflow_and_price_confirmation():
    c = make_db(); seed_chips(c, "8150")
    seed_snap(c, "8150", "南茂", "10:59:00", 93.9, 13107, 5.2, 92.8)
    seed_snap(c, "8150", "南茂", "11:41:00", 94.6, 15779, 6.0, 93.1)
    c.commit()
    card = build_reversal_day1(c, "2026-09-01")["results"][0]
    assert card["symbol"] == "8150"
    assert card["r1_prior_outflow"] is True
    assert card["r3_aflow_flip"] is True
    assert card["r4_aflow_persistence"] is True
    assert card["r5_price_confirmation"] is True
    assert card["aflow_delta"] == 2672
    assert round(card["price_delta"], 1) == 0.7
    assert card["state"] == "REVERSAL_PRIORITY"
    assert card["action"] == "OBSERVE_ONLY"


def test_3532_like_case_is_priority_even_with_lower_absolute_aflow():
    c = make_db(); seed_chips(c, "3532")
    seed_snap(c, "3532", "台勝科", "10:59:00", 400.5, 604, 5.5, 398)
    seed_snap(c, "3532", "台勝科", "11:41:00", 405.0, 1061, 6.7, 401)
    c.commit()
    card = build_reversal_day1(c, "2026-09-01")["results"][0]
    assert card["r4_aflow_persistence"] is True
    assert card["state"] == "REVERSAL_PRIORITY"
    assert card["aflow_delta"] == 457


def test_3374_negative_aflow_is_not_day1_reversal():
    c = make_db(); seed_chips(c, "3374")
    seed_snap(c, "3374", "精材", "11:41:00", 421, -1322, 5.0, 419)
    c.commit()
    card = build_reversal_day1(c, "2026-09-01")["results"][0]
    assert card["r3_aflow_flip"] is False
    assert card["state"] == "OUTFLOW_REVERSAL_WATCH"


def test_8358_previous_flow_reversal_then_price_failure_is_separate_control_state():
    c = make_db()
    dates = [f"2026-08-{d:02d}" for d in range(9, 29)]
    for i, d in enumerate(dates):
        inst = 1000 if i >= 15 else -2000
        c.execute("INSERT INTO chip_daily VALUES(?,?,?,?,?,?,?,?)",
                  (d, "8358", inst/2, inst, 10000, .1, d, d+"T16:00:00+08:00"))
    seed_snap(c, "8358", "金居", "13:21:00", 528, -1200, -2.22, 540.16)
    c.execute("INSERT INTO trigger_context VALUES(?,?,?,?,?,?,?,?,?)",
              ("2026-08-31","8358",None,None,None,None,"2026-08-31","2026-08-31T15:10:00+08:00",
               "CONTROL: 20D flow negative, latest 5D already positive; financing 20D average 396.82 and price about 36.1% above it on 8/31."))
    c.commit()
    card = next(x for x in build_reversal_day1(c, "2026-09-01")["results"] if x["symbol"] == "8358")
    assert card["institutional_net_20d"] < 0
    assert card["institutional_net_5d"] > 0
    assert card["above_vwap"] is False
    assert card["state"] == "PREVIOUS_FLOW_REVERSAL_DAY2_3_FAILURE"


def test_3026_outflow_watch_without_day1_trigger_is_not_buy_signal():
    c = make_db()
    dates = [f"2026-08-{d:02d}" for d in range(9, 29)]
    for i, d in enumerate(dates):
        inst = 500 if i == 19 else -1000
        c.execute("INSERT INTO chip_daily VALUES(?,?,?,?,?,?,?,?)",
                  (d, "3026", inst/2, inst, 10000, .1, d, d+"T16:00:00+08:00"))
    seed_snap(c, "3026", "禾伸堂", "13:21:00", 731, -300, -1.48, 763.80)
    c.commit()
    card = next(x for x in build_reversal_day1(c, "2026-09-01")["results"] if x["symbol"] == "3026")
    assert card["institutional_net_20d"] < 0
    assert card["institutional_net_5d"] < 0
    assert card["r2_price_reversal"] is False
    assert card["above_vwap"] is False
    assert card["state"] == "OUTFLOW_WATCH_NOT_TRIGGERED"


def test_2303_control_keeps_strong_aflow_but_does_not_fake_reversal_without_5d_20d_history():
    c = make_db()
    c.execute("INSERT INTO chip_daily VALUES(?,?,?,?,?,?,?,?)",
              ("2026-08-31", "2303", -12716, -7502, None, None,
               "2026-08-31", "2026-08-31T16:00:00+08:00"))
    seed_snap(c, "2303", "聯電", "11:00:00", 129, 17963, 0.0, 129, volume=165000)
    c.execute("INSERT INTO trigger_context VALUES(?,?,?,?,?,?,?,?,?)",
              ("2026-08-31", "2303", 129, 129, 0, 1, "2026-08-31",
               "2026-08-31T15:10:00+08:00",
               "CONTROL: RVOL 1.65x; A-flow +17,963; trigger 129 hit; Acceptance FAILED; final action NO_ENTRY"))
    c.commit()
    card = next(x for x in build_reversal_day1(c, "2026-09-01")["results"] if x["symbol"] == "2303")
    assert card["current_aflow"] == 17963
    assert card["r1_prior_outflow"] is False
    assert card["state"] == "NOT_REVERSAL"
    assert "Acceptance FAILED" in card["control_note"]


def test_stale_price_never_becomes_reversal_signal():
    c = make_db(); seed_chips(c, "6182")
    seed_snap(c, "6182", "合晶", "10:20:00", 109, 5000, 3.3, 107)
    c.execute("UPDATE intraday_snapshot SET price_data_date='2026-08-31' WHERE symbol='6182'")
    c.commit()
    card = build_reversal_day1(c, "2026-09-01")["results"][0]
    assert card["state"] == "STALE_PRICE_DATA"
    assert card["action"] == "OBSERVE_ONLY"


def test_reversal_endpoint_and_tab_are_standalone(tmp_path):
    app = create_app(tmp_path / "preview.db")
    client = TestClient(app)
    r = client.get("/api/flow-chips/reversal-day1?trade_date=2026-09-01")
    assert r.status_code == 200
    assert r.json()["research_only"] is True
    assert "反轉 DAY-1" in client.get("/").text
