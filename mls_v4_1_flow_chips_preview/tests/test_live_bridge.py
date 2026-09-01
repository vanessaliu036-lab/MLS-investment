from mls_v4_1_flow_chips.live_bridge import build_live_view


def row(code, name, *, price, avg, change, aflow, vol, f5, f20, fd=0, chase=False):
    return {
        "code": code, "name": name, "sector": "TEST", "price": price, "high": price+1,
        "low": price-1, "avg_price": avg, "change_rate": change, "aflow": aflow,
        "total_volume": vol, "price_source": "shioaji", "quote_status": "LIVE",
        "aflow_status": "LIVE", "pre_activation": {
            "foreign_net_d": fd, "foreign_net_5d": f5, "foreign_net_20d": f20,
            "foreign_source": "FinMind TaiwanStockInstitutionalInvestorsBuySell",
            "foreign_source_date": "2026-08-31", "do_not_chase": chase,
            "ma5_distance_pct": 2.0, "volume_ratio": 1.2,
        }
    }


def test_live_view_uses_aflow_for_ranking_and_labels_foreign_periods():
    p = {"ok": True, "rows": [
        row("A", "A", price=105, avg=100, change=3, aflow=1000, vol=10000, f5=500, f20=1000),
        row("B", "B", price=101, avg=100, change=1, aflow=2000, vol=10000, f5=500, f20=1000),
    ]}
    v = build_live_view(p)
    assert v["inflow"][0]["symbol"] == "B"
    assert v["inflow"][0]["foreign_net_5d"] == 500
    assert v["inflow"][0]["foreign_source_date"] == "2026-08-31"


def test_extreme_outflow_plus_price_and_aflow_reversal_is_early_not_priority_without_history():
    p = {"ok": True, "rows": [row("6182", "合晶", price=112, avg=110, change=6.6, aflow=8872, vol=50000, f5=-15014, f20=-71903)]}
    c = build_live_view(p)["reversal"][0]
    assert c["reversal_state"] == "REVERSAL_DAY1_EARLY"
    assert c["reversal_persistence"] == "NO_DATA"
    assert "PERSISTENCE_NO_DATA" in c["reversal_reason_codes"]


def test_previous_reversal_day23_failure():
    p = {"ok": True, "rows": [row("8358", "金居", price=526, avg=539, change=-2.6, aflow=-4155, vol=30000, f5=7146, f20=-19567)]}
    c = build_live_view(p)["reversal"][0]
    assert c["reversal_state"] == "PREVIOUS_FLOW_REVERSAL_DAY2_3_FAILURE"


def test_outflow_watch_not_triggered():
    p = {"ok": True, "rows": [row("3026", "禾伸堂", price=731, avg=761, change=-1.48, aflow=-3045, vol=12000, f5=110, f20=-11047)]}
    c = build_live_view(p)["reversal"][0]
    assert c["reversal_state"] == "PREVIOUS_FLOW_REVERSAL_DAY2_3_FAILURE"
    # 5D is slightly positive in the verified VPS payload, so this is not a pure
    # 5D+20D outflow case. It is correctly treated as a recent-flow-turn control.


def test_strong_prior_chips_with_intraday_outflow_is_no_entry():
    p = {"ok": True, "rows": [row("2408", "南亞科", price=518, avg=525, change=-4.6, aflow=-26220, vol=78898, f5=39733, f20=168182)]}
    c = build_live_view(p)["outflow"][0]
    assert c["flow_state"] == "STRONG_CHIP_INTRADAY_OUTFLOW"
    assert c["action"] == "NO_ENTRY"
