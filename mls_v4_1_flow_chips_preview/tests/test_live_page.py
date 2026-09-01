from mls_v4_1_flow_chips.live_run import render_html


def test_page_is_named_reversal_lab_and_shows_forward_test_pipeline():
    card = {
        "symbol": "6182", "name": "合晶", "sector": "晶圓材料",
        "price": 116.0, "change_rate": 9.95, "aflow": 32605.0,
        "aflow_ratio": 0.4528, "avg_price": 113.01,
        "flow_state": "REVERSAL_DAY1_EARLY", "action": "NO_CHASE",
        "reason_codes": ["A_FLOW_POSITIVE"],
        "lab_role": "REVERSAL_DAY1", "reversal_grade": "A+",
        "reversal_state": "REVERSAL_DAY1_EARLY_EXTENDED",
        "reversal_reason_codes": ["A_FLOW_FLIPPED", "PERSISTENCE_NO_DATA"],
        "flow_persistence": "NO_DATA", "price_confirmation": "CONFIRMED",
        "sector_confirmation": "CONFIRMED", "day2_ready": "PENDING_PERSISTENCE",
        "foreign_net_d": -3452.0, "foreign_net_5d": -15014.0,
        "foreign_net_20d": -71903.0,
        "foreign_source": "FinMind TaiwanStockInstitutionalInvestorsBuySell",
        "foreign_source_date": "2026-08-31", "price_source": "shioaji",
        "quote_status": "LIVE",
    }
    view = {
        "lab_name": "資金反轉驗證 / Reversal Lab",
        "model_scope": "FORWARD_TEST_ONLY",
        "updated_at": "2026-09-01T13:30:00+08:00",
        "inflow": [card], "outflow": [], "reversal": [card],
    }
    page = render_html(view)
    assert "資金反轉驗證 / Reversal Lab" in page
    assert "FORWARD TEST ONLY" in page
    assert "不影響正式 Trend / Entry" in page
    assert "Flow Flip" in page
    assert "Flow Persistence" in page
    assert "Price Confirmation" in page
    assert "Sector Confirmation" in page
    assert "Day-2 Ready" in page
    assert "A+" in page
