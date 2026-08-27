import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import line_b_explain as explain
import line_b_ledger_render as render
import line_b_layers as layers
import line_b_layers_render as layers_render


def _row(**overrides):
    row = {
        "t1_prior_high": 456.5,
        "current_price": 447.0,
        "t1_price_5d": 440.0,
        "t1_close_position": 0.8,
        "t1_inst_5d": 1200,
        "flow_class": "FLOW_FLIP",
        "flow_confirm_magnitude": 543,
        "watch_mode_activated": True,
    }
    row.update(overrides)
    return row


def test_flow_confirmation_does_not_claim_price_is_above_resistance():
    exp = explain.explain(_row())

    assert exp["status"] == "CONFIRMED"
    assert exp["distance_pct"] == -2.08
    assert "A-flow 已確認" in exp["system_sentence"]
    assert "尚差關鍵價" in exp["system_sentence"]
    assert "已站上關鍵價" not in exp["system_sentence"]

    prob = render._prob_block(exp, discovery=False)
    assert "A-flow 已確認" in prob
    assert ">已站上<" not in prob


def test_price_above_resistance_may_claim_price_is_above():
    exp = explain.explain(_row(current_price=460.0))

    assert exp["distance_pct"] > 0
    assert "已站上關鍵價" in exp["system_sentence"]
    assert "PRICE TRIGGER 已發生" in exp["system_sentence"]
    assert "待量能／承接確認" in exp["system_sentence"]
    assert "啟動已發生" not in exp["system_sentence"]
    assert "尚差關鍵價" not in exp["system_sentence"]
    assert ">已站上<" in render._prob_block(exp, discovery=False)


def test_intraday_discovery_uses_two_column_grid_on_desktop():
    page = render.render({
        "has_data": True,
        "data_date": "2026-08-27",
        "is_live": True,
        "labels": {"c1_c2_rate": "64.1%", "flow_confirmed_rate": "89.9%",
                   "flow_no_flip_rate": "2.8%", "sample_note": "test"},
        "c1_c2_list": [],
        "flow_confirmed_top3": [],
        "intraday_discovery": [],
    })
    assert '.discovery-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}' in page
    assert '<div class="discovery-grid">' in page
    assert '.grid,.discovery-grid{grid-template-columns:1fr}' in page


def _layer_row(state, flow, change, distance):
    return {
        "state": {"state": state},
        "flow": {"net_active": flow},
        "extension": {"change_rate": change},
        "distance_pct": distance,
    }


def test_seven_layer_rows_follow_state_then_flow_change_and_distance():
    rows = [
        _layer_row("WATCH", 9999, 9.0, 2.0),
        _layer_row("ARMED", 1, -1.0, -1.0),
        _layer_row("ARMED", 100, 1.0, 1.0),
        _layer_row("REJECT", 99999, 9.0, 9.0),
    ]
    ordered = sorted(rows, key=layers.display_sort_key)
    assert [row["state"]["state"] for row in ordered] == ["ARMED", "ARMED", "WATCH", "REJECT"]
    assert ordered[0]["flow"]["net_active"] == 100


def test_seven_layer_page_has_clickable_state_filters_and_clear_price_labels():
    row = {
        "code": "2408", "name": "南亞科", "price": 547.0, "trigger_price": 522.0,
        "distance_pct": 4.79,
        "chip": {"total_5d": 24399, "foreign_5d": 23546, "foreign_days": 4,
                 "verdict": "CONFIRMED", "summary": "三方皆偏正"},
        "flow": {"verdict": "STRONG", "net_active": 7391},
        "trigger": {"verdict": "YES", "hold_minutes": 165},
        "volume": {"verdict": "THIN", "rvol": 1.0, "rvol_base_days": 17,
                   "turnover_pct": 2.03},
        "acceptance": {"verdict": "NO", "held_minutes": 165, "max_drawdown_pct": 2.34},
        "extension": {"verdict": "HIGH", "reasons": ["距MA20 +15.5%"],
                      "gap_pct": None, "change_rate": 4.79},
        "sector": {"verdict": "STRONG", "breadth_pct": 100, "group": "記憶體",
                   "leadership": True},
        "state": {"state": "ARMED", "action": "等站穩確認", "why": "量能尚薄"},
        "freshness": {"inst_flow_through": "2026-08-26", "t1_bar_date": "2026-08-26",
                      "quote_updated_at": "2026-08-27T12:00:00",
                      "aflow_updated_at": "2026-08-27T12:00:00"},
    }
    page = layers_render.render({
        "rows": [row], "counts": {"ARMED": 1}, "T": "2026-08-27",
        "observation_version": "test",
    })
    for text in ('data-state-filter="ALL"', 'data-state-filter="ARMED"',
                 'data-state="ARMED"', '股票／現價／漲跌／觸發',
                 '現價 <b class="num-up">547.00</b>',
                 '漲跌 <b class="num-up">+4.79%</b>'):
        assert text in page
