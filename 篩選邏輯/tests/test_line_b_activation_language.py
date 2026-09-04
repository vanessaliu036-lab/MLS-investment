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
    assert "尚差關鍵價" not in exp["system_sentence"]
    assert "已站上關鍵價" not in exp["system_sentence"]

    prob = render._prob_block(exp, discovery=False)
    assert "A-flow 已確認" in prob
    assert ">已站上<" not in prob


def test_price_above_resistance_may_claim_price_is_above():
    exp = explain.explain(_row(current_price=460.0))

    assert exp["distance_pct"] > 0
    assert exp["status"] == "PRICE_TRIGGERED"
    assert exp["monitor_bucket"] == "PRICE_TRIGGERED"
    assert "已站上關鍵價" not in exp["system_sentence"]
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
    assert '.monitor-radar{display:grid;gap:18px}' in page
    assert '.grid,.discovery-grid{grid-template-columns:1fr}' in page


def test_monitor_page_has_tabs_for_requested_buy_point_buckets():
    page = render.render({
        "has_data": True,
        "data_date": "2026-08-27",
        "is_live": True,
        "labels": {"c1_c2_rate": "64.1%", "flow_confirmed_rate": "89.9%",
                   "flow_no_flip_rate": "2.8%", "sample_note": "test"},
        "c1_c2_list": [],
        "flow_confirmed_top3": [],
        "intraday_discovery": [],
        "monitor_sections": [
            {"bucket": "PRICE_TRIGGERED", "label": "PRICE TRIGGER 已發生", "rows": []},
            {"bucket": "CONFIRMED", "label": "A-flow 已確認", "rows": []},
            {"bucket": "WAITING_FUNDS", "label": "等待資金", "rows": []},
            {"bucket": "DISCOVERY", "label": "盤中發現", "rows": []},
        ],
    })
    assert '<div class="monitor-tabs" role="tablist"' in page
    assert 'data-monitor-tab="PRICE_TRIGGERED"' in page
    assert "PRICE TRIGGER 已發生" in page
    assert 'data-monitor-tab="CONFIRMED"' in page
    assert 'data-monitor-tab="WAITING_FUNDS"' in page
    assert 'data-monitor-tab="DISCOVERY"' in page
    assert 'data-monitor-panel="CONFIRMED"' in page
    assert 'function selectMonitorTab' in page


def test_stock_card_shows_price_change_pct_next_to_name():
    exp = explain.explain({
        "t1_close": 100.0, "t1_prior_high": 105.0, "current_price": 102.3,
        "flow_class": "OPEN_POSITIVE", "flow_confirm_magnitude": 500,
        "watch_mode_activated": 0,
    }, is_eod=False)
    assert exp["change_pct"] == 2.3
    card = render._stock_card({"code": "2464", "monitor_bucket": "CONFIRMED",
                               "flow_confirm_magnitude": 500, "explain": exp})
    assert 'class="price-change up"' in card
    assert "+2.30%" in card


def test_confirmed_states_use_distinct_price_and_flow_colors():
    above = explain.explain({
        "t1_close": 100.0, "t1_prior_high": 105.0, "current_price": 106.0,
        "flow_class": "OPEN_POSITIVE", "watch_mode_activated": 1,
    }, is_eod=False)
    below = explain.explain({
        "t1_close": 100.0, "t1_prior_high": 105.0, "current_price": 103.0,
        "flow_class": "OPEN_POSITIVE", "watch_mode_activated": 0,
    }, is_eod=False)
    above_block = render._prob_block(above, discovery=False,
                                     monitor_bucket="PRICE_TRIGGERED")
    below_block = render._prob_block(below, discovery=False,
                                     monitor_bucket="CONFIRMED")
    assert 'prob-num price-trigger' in above_block
    assert 'bar price-trigger' in above_block
    assert 'prob-num flow-confirmed' in below_block
    assert 'bar flow-confirmed' in below_block


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


def test_seven_layer_page_renders_trade_judgment_without_stock_navigation():
    row = {
        "code": "5380", "name": "示範股", "price": 538.0, "trigger_price": 533.0,
        "distance_pct": 0.94,
        "chip": {"total_5d": 5000, "foreign_5d": 3867, "foreign_days": 2,
                 "verdict": "CONFIRMED", "summary": "三方皆偏正"},
        "flow": {"verdict": "STRONG", "net_active": 3867},
        "trigger": {"verdict": "YES", "hold_minutes": 15},
        "volume": {"verdict": "PASS", "rvol": 1.8, "rvol_base_days": 11,
                   "turnover_pct": 2.03},
        "acceptance": {"verdict": "YES", "held_minutes": 15, "max_drawdown_pct": 0.4},
        "extension": {"verdict": "HIGH", "reasons": ["today +2.7%"],
                      "gap_pct": None, "change_rate": 2.67},
        "sector": {"verdict": "STRONG", "breadth_pct": 70, "group": "半導體",
                   "leadership": True},
        "state": {"state": "EXTENDED", "action": "小部位可追", "why": "主升續攻中",
                  "trend_stage": "主升續攻", "flow_state": "持續",
                  "chase_permission": "小部位可追", "entry_method": "VWAP承接",
                  "failure_conditions": ["跌 VWAP", "A-flow 翻負", "爆量滯漲", "跌破關鍵價"]},
        "freshness": {"inst_flow_through": "2026-08-26", "t1_bar_date": "2026-08-26",
                      "quote_updated_at": "2026-08-27T12:00:00",
                      "aflow_updated_at": "2026-08-27T12:00:00"},
    }
    page = layers_render.render({"rows": [row], "counts": {"EXTENDED": 1},
                                 "T": "2026-08-27", "observation_version": "test"})

    assert "追價許可" in page
    assert "主升續攻" in page
    assert "小部位可追" in page
    assert "api/card_page?code=" not in page


def test_armed_page_explains_failure_thresholds_without_calling_stock_card():
    row = {
        "code": "2303", "name": "聯電", "price": 128.5, "trigger_price": 129.0,
        "distance_pct": -0.39,
        "chip": {"total_5d": 34430, "foreign_5d": 19236, "foreign_days": 1,
                 "verdict": "CONFIRMED", "summary": "三方皆偏正"},
        "flow": {"verdict": "STRONG", "net_active": 10837},
        "trigger": {"verdict": "NO", "hold_minutes": 5, "hold_slots": 1,
                    "above_vwap": False, "vwap": 128.94},
        "volume": {"verdict": "THIN", "rvol": 0.9, "rvol_base_days": 23,
                   "turnover_pct": 0.49},
        "acceptance": {"verdict": "NO", "held_minutes": 5, "held_slots": 1,
                        "max_drawdown_pct": 1.15, "vwap_held": False},
        "extension": {"verdict": "NORMAL", "reasons": [],
                      "gap_pct": None, "change_rate": 2.8},
        "sector": {"verdict": "STRONG", "breadth_pct": 67, "group": "功率半導體",
                   "leadership": True},
        "state": {"state": "ARMED", "action": "等回踩", "why": "距觸發價 -0.39%",
                  "trend_stage": "準備啟動", "flow_state": "增強",
                  "chase_permission": "等回踩", "entry_method": "資金再加速",
                  "failure_conditions": ["跌 VWAP", "A-flow 翻負", "爆量滯漲", "跌破關鍵價"],
                  "failure_alerts": []},
        "freshness": {"inst_flow_through": "2026-09-03", "t1_bar_date": "2026-09-03",
                      "quote_updated_at": "2026-09-04T10:03:00",
                      "aflow_updated_at": "2026-09-04T10:03:00"},
    }
    page = layers_render.render({"rows": [row], "counts": {"ARMED": 1},
                                 "T": "2026-09-04", "observation_version": "test"})

    assert "跌 VWAP（已啟動後現價 &lt; VWAP 128.94）" in page
    assert "跌破關鍵價（已啟動後現價 &lt; 觸發價 129.00）" in page
    assert "準備啟動" in page
    assert "準備啟動，不列為失敗" in page
    assert "文字可反白選取複製" in page
    assert "role=\"link\"" not in page
    assert 'data-stock-code="2303"' not in page
    assert "window.location.href = '/api/card_page?code='" not in page
