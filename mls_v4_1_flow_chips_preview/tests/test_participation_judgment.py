"""Regression tests for the Reversal Lab participation decision.

The lab must preserve an actionable continuation signal when price is already
extended.  Extension is a sizing/entry concern; it is not, by itself, a
failure condition.
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from mls_v4_1_flow_chips.live_bridge import build_participation_judgment
from mls_v4_1_flow_chips.live_bridge import build_live_view
from mls_v4_1_flow_chips.live_run import render_html


def test_high_position_and_three_up_days_keep_main_rise_participation():
    result = build_participation_judgment({
        "aflow": 3867,
        "aflow_ratio": 0.1488,
        "change_rate": 2.67,
        "above_vwap_proxy": True,
        "ma5_distance_pct": 9.2,
        "consecutive_up_days": 3,
        "volume_ratio": 1.1,
    })

    assert result["trend_stage"] == "MAIN_UPTREND_CONTINUATION"
    assert result["capital_state"] == "STRENGTHENING"
    assert result["chase_permission"] == "SMALL_SIZE_CHASE"
    assert result["entry_method"] == "VWAP_SUPPORT"
    assert all(not item["active"] for item in result["failure_conditions"])


def test_flow_acceleration_allows_breakout_chase():
    result = build_participation_judgment({
        "aflow": 6000,
        "aflow_ratio": 0.20,
        "change_rate": 5.0,
        "above_vwap_proxy": True,
        "volume_ratio": 1.4,
        "ma5_distance_pct": 12.0,
        "consecutive_up_days": 3,
    })

    assert result["trend_stage"] == "ACCELERATION_ATTACK"
    assert result["chase_permission"] == "CHASE_BREAKOUT"
    assert result["entry_method"] == "BREAKOUT_CHASE"


def test_only_real_failure_conditions_turn_off_participation():
    result = build_participation_judgment({
        "aflow": -1200,
        "aflow_ratio": -0.08,
        "change_rate": -1.8,
        "above_vwap_proxy": False,
        "volume_ratio": 1.6,
    })

    assert result["trend_stage"] == "EXHAUSTION_FAILURE"
    assert result["chase_permission"] == "DO_NOT_CHASE"
    assert result["failure_conditions"] == [
        {"key": "BELOW_VWAP", "active": True},
        {"key": "A_FLOW_TURNED_NEGATIVE", "active": True},
        {"key": "VOLUME_STALL", "active": True},
        {"key": "KEY_PRICE_BREAK", "active": False},
    ]


def test_reversal_lab_explains_participation_rule():
    page = render_html({
        "updated_at": "2026-09-02T10:00:00+08:00",
        "state_summary": {},
        "state_groups": {},
        "reversal": [{
            "symbol": "5380", "name": "示範股", "sector": "半導體",
            "price": 538, "change_rate": 2.67, "aflow": 3867,
            "aflow_ratio": 0.1488, "avg_price": 533.61,
            "flow_state": "FLOW_POSITIVE", "action": "WATCH",
            "reason_codes": [], "lab_role": "OTHER_CONTROL",
            "failure_conditions": [
                {"key": "BELOW_VWAP", "active": False},
                {"key": "A_FLOW_TURNED_NEGATIVE", "active": False},
                {"key": "VOLUME_STALL", "active": False},
                {"key": "KEY_PRICE_BREAK", "active": False},
            ],
            **build_participation_judgment({
                "aflow": 3867, "aflow_ratio": 0.1488,
                "change_rate": 2.67, "above_vwap_proxy": True,
            }),
        }],
    })

    assert "現在能不能參與" in page
    assert "高位、連漲三天、乖離高都不是淘汰條件" in page
    assert "固定輸出：趨勢階段／資金狀態／追價許可／進場方式／失敗條件" in page
    assert 'data-target=\'participation\'' in page
    assert "主升續攻" in page
    assert "不追價" not in page


def test_live_view_exposes_participation_as_a_separate_contract():
    view = build_live_view({"rows": [], "updated_at": "2026-09-02T10:00:00+08:00"})

    assert view["participation"] is view["reversal"]
