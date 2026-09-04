"""line_b_layers.trade_state() 的 CHIP 覆寫 + _volume_ever_passed() 補充測試。

ACTIVE/EXTENDED/FAILED 的生命週期已在 test_line_b.py 覆蓋
(test_extension_does_not_block_active_but_overrides_action /
test_failed_when_price_falls_back_below_trigger /
test_not_failed_when_trigger_only_briefly_touched_without_real_activation);
這裡只補兩塊還沒測到的:
  1. trade_state() 本身的 CHIP BEARISH 覆寫(即使 Trigger+Volume+Acceptance
     全過,籌碼明顯偏空也只能到 ARMED,不得 ACTIVE/可操作)。
  2. _volume_ever_passed() 這支歷史回看小函式的邊界行為。
純函式測試,不碰 DB。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import line_b_layers as layers


def test_bearish_chip_caps_active_at_armed_not_entry_eligible():
    """5483/6182 型態:Trigger+Volume+Acceptance 全過,但法人籌碼明顯偏空 →
    最高只能 ARMED/WAIT,不得 ACTIVE/可操作(Vanessa 規格第七條)。"""
    chip = {"verdict": "BEARISH"}
    flow = {"verdict": "STRONG"}
    trig = {"verdict": "YES", "hold_slots": 3}
    vol = {"verdict": "PASS"}
    acc = {"verdict": "YES", "held_slots": 3, "vwap_held": True}
    ext = {"verdict": "NORMAL", "reasons": []}
    st = layers.trade_state(chip, flow, trig, vol, acc, ext, structure_ok=True,
                            distance_pct=2.0, volume_ever_passed=True)
    assert st["state"] == "ARMED"
    assert st["action_code"] == "WAIT"
    assert st["state"] != "ACTIVE"


def test_volume_ever_passed_detects_pass_only_inside_initial_breakout_window():
    trigger = 100.0
    hist_by_slot = {"0930": [1000.0], "0935": [1000.0], "0940": [1000.0]}
    slots = [
        {"price": 99.0, "volume": 500.0, "slot": "0925"},   # 尚未突破
        {"price": 101.0, "volume": 900.0, "slot": "0930"},  # 突破,量不足(RVOL<1.5)
        {"price": 102.0, "volume": 1600.0, "slot": "0935"}, # 仍在窗內,量達標
        {"price": 98.0, "volume": 1700.0, "slot": "0940"},  # 已跌破,不該再看
    ]
    assert layers._volume_ever_passed(slots, trigger, hist_by_slot) is True

    # 若量能達標只發生在窗口關閉(跌破)之後,不算「突破時曾帶量」
    slots2 = [
        {"price": 99.0, "volume": 500.0, "slot": "0925"},
        {"price": 101.0, "volume": 900.0, "slot": "0930"},
        {"price": 98.0, "volume": 1000.0, "slot": "0935"},   # 已跌破
        {"price": 97.0, "volume": 5000.0, "slot": "0940"},   # 破位後才爆量,不算
    ]
    assert layers._volume_ever_passed(slots2, trigger, hist_by_slot) is False


def test_main_rise_keeps_small_position_permission_when_extension_is_high():
    """高位延伸只縮小部位，不把主升續攻誤判成禁止交易。"""
    flow = layers.flow_layer(3867, [
        {"net_active": 2500}, {"net_active": 3867},
    ])
    st = layers.trade_state(
        {"verdict": "CONFIRMED"}, flow,
        {"verdict": "YES", "hold_slots": 3, "above_vwap": True},
        {"verdict": "PASS", "vol_accel": 1.0},
        {"verdict": "YES", "held_slots": 3, "vwap_held": True},
        {"verdict": "HIGH", "reasons": ["today +2.7%"]},
        True, 0.8, volume_ever_passed=True,
    )

    assert st["trend_stage"] == "主升續攻"
    assert st["flow_state"] == "增強"
    assert st["chase_permission"] == "小部位可追"
    assert st["entry_method"] == "VWAP承接"
    assert st["failure_conditions"] == ["跌 VWAP", "A-flow 翻負", "爆量滯漲", "跌破關鍵價"]


def test_acceleration_attack_allows_breakout_chase():
    """A-flow 增量且價格突破前高時，狀態升級為可追突破。"""
    flow = layers.flow_layer(6000, [
        {"net_active": 3867}, {"net_active": 6000},
    ])
    st = layers.trade_state(
        {"verdict": "CONFIRMED"}, flow,
        {"verdict": "YES", "hold_slots": 2, "above_vwap": True,
         "above_intraday_prior_high": True},
        {"verdict": "PASS", "vol_accel": 1.4},
        {"verdict": "YES", "held_slots": 2, "vwap_held": True},
        {"verdict": "NORMAL", "reasons": []},
        True, 1.2, volume_ever_passed=True,
    )

    assert st["trend_stage"] == "加速攻擊"
    assert st["chase_permission"] == "可追"
    assert st["entry_method"] == "突破追"


def test_armed_preparation_does_not_report_breakout_as_failure():
    """短暫碰到觸發價後回落，但尚未完成量能/承接確認，仍是準備啟動。"""
    st = layers.trade_state(
        {"verdict": "CONFIRMED"},
        {"verdict": "STRONG", "flow_state": "增強"},
        {"verdict": "NO", "hold_slots": 1, "above_vwap": False},
        {"verdict": "THIN"},
        {"verdict": "NO", "held_slots": 1, "vwap_held": False},
        {"verdict": "NORMAL", "reasons": []},
        True, -0.39, volume_ever_passed=False,
    )

    assert st["state"] == "ARMED"
    assert st["trend_stage"] == "準備啟動"
    assert "跌破關鍵價" not in st["failure_alerts"]


def test_failed_vwap_does_not_report_key_price_break_when_still_above_trigger():
    """已突破但跌破 VWAP 時，只報 VWAP 失效，不誤報跌破關鍵價。"""
    st = layers.trade_state(
        {"verdict": "CONFIRMED"},
        {"verdict": "STRONG", "flow_state": "持續"},
        {"verdict": "YES", "hold_slots": 3, "above_vwap": False},
        {"verdict": "PASS"},
        {"verdict": "NO", "held_slots": 3, "vwap_held": False},
        {"verdict": "NORMAL", "reasons": []},
        True, 0.5, volume_ever_passed=True,
    )

    assert st["state"] == "FAILED"
    assert "跌 VWAP" in st["failure_alerts"]
    assert "跌破關鍵價" not in st["failure_alerts"]
