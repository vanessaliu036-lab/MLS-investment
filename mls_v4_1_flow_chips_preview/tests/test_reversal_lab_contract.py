from mls_v4_1_flow_chips.live_bridge import build_live_view


def r(code, name, sector, price, avg, change, aflow, vol, f5, f20, *, limit=False, chase=False):
    return {
        "code": code, "name": name, "sector": sector,
        "price": price, "high": max(price, avg) + 1, "low": min(price, avg) - 1,
        "avg_price": avg, "change_rate": change, "aflow": aflow,
        "total_volume": vol, "is_limit_up": limit, "quadrant": "真攻擊" if aflow > 0 else "休息",
        "price_source": "shioaji", "quote_status": "LIVE", "aflow_status": "LIVE",
        "pre_activation": {
            "foreign_net_d": 0, "foreign_net_5d": f5, "foreign_net_20d": f20,
            "foreign_source": "FinMind TaiwanStockInstitutionalInvestorsBuySell",
            "foreign_source_date": "2026-08-31", "do_not_chase": chase,
            "foreign_days": -1, "ma5_distance_pct": 0, "volume_ratio": 1,
        },
    }


def six_payload():
    return {"ok": True, "rows": [
        r("6182", "合晶", "晶圓材料", 116.0, 113.01, 9.95, 32605, 72007, -15014, -71903, limit=True, chase=True),
        r("8150", "南茂", "封測", 93.2, 93.61, 4.60, 13129, 48907, -2801, -27150),
        r("3532", "台勝科", "晶圓材料", 402.0, 399.28, 6.06, 643, 8883, -2679, -48, chase=True),
        r("3026", "禾伸堂", "被動元件", 731.0, 761.47, -1.48, -3065, 12531, 110, -11047),
        r("8358", "金居", "PCB材料", 526.0, 538.97, -2.59, -4077, 30425, 7146, -19567, chase=True),
        r("2408", "南亞科", "記憶體", 518.0, 525.08, -4.60, -26475, 78643, 39733, 168182),
    ]}


def cards():
    v = build_live_view(six_payload(), top_n=10)
    return {c["symbol"]: c for c in (v["inflow"] + v["outflow"] + v["reversal"])}


def test_6182_is_a_plus_reversal_day1():
    c = cards()["6182"]
    assert c["lab_role"] == "REVERSAL_DAY1"
    assert c["reversal_grade"] == "A+"


def test_8150_stays_a_day1_even_if_close_finishes_slightly_below_vwap():
    c = cards()["8150"]
    assert c["lab_role"] == "REVERSAL_DAY1"
    assert c["reversal_grade"] == "A"
    assert c["price_confirmation"] == "WEAKENED"


def test_3532_is_b_plus_day1():
    c = cards()["3532"]
    assert c["lab_role"] == "REVERSAL_DAY1"
    assert c["reversal_grade"] == "B+"


def test_3026_tiny_positive_5d_does_not_count_as_prior_flow_reversal():
    c = cards()["3026"]
    assert c["lab_role"] == "OUTFLOW_WATCH"
    assert c["reversal_state"] == "OUTFLOW_WATCH_NOT_TRIGGERED"


def test_8358_is_previous_reversal_continuation_failure():
    c = cards()["8358"]
    assert c["lab_role"] == "REVERSAL_FAILURE_CONTROL"
    assert c["reversal_state"] == "PREVIOUS_FLOW_REVERSAL_DAY2_3_FAILURE"


def test_2408_is_trend_control_not_reversal():
    c = cards()["2408"]
    assert c["lab_role"] == "TREND_CONTROL"
    assert c["reversal_state"] == "NOT_REVERSAL"
