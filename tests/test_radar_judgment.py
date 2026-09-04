"""機會雷達盤中判讀：方向、資金性質與可買價格位置分責。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(MODULE_DIR))

import vps_intraday_test as vit  # noqa: E402


def _quote(*, price, change, flow, avg=100, trigger=100, quadrant=None):
    return {
        "price": price,
        "change_rate": change,
        "aflow": flow,
        "avg_price": avg,
        "trigger_price": trigger,
        "quadrant": quadrant,
    }


def test_in_zone_requires_price_position_in_addition_to_true_attack():
    got = vit._radar_judgment(
        _quote(price=101, change=1.0, flow=500, quadrant="真攻擊"),
    )

    assert got["status"] == "可進場"
    assert got["price_position"]["state"] == "IN_ZONE"
    assert got["price_gate"] is True


def test_extended_attack_waits_for_explicit_trigger_price_pullback():
    got = vit._radar_judgment(
        _quote(price=106, change=6.0, flow=800, quadrant="真攻擊"),
        extension_state="EXTENDED",
    )

    assert got["status"] == "等回測"
    assert "回測關鍵價 100" in got["next_step"]
    assert "A-flow 維持正值" in got["next_step"]


def test_price_up_and_flow_out_is_inducement_high_and_skip():
    got = vit._radar_judgment(
        _quote(price=108, change=5.0, flow=-300, quadrant="假紅"),
    )

    assert got["status"] == "不進場"
    assert "價量背離" in got["reason"]
    assert got["next_step"].startswith("不追價")


def test_flow_in_on_price_down_is_healthy_rotation_to_observe_not_buy():
    got = vit._radar_judgment(
        _quote(price=98, change=-1.0, flow=400, quadrant="惜售"),
    )

    assert got["status"] == "保留觀察"
    assert "主動資金流入但價格尚未止穩" in got["reason"]
    assert "價格止跌" in got["next_step"]
    assert got["money_gate"] is True


def test_positive_flow_below_trigger_is_candidate_with_signal_to_wait_for():
    got = vit._radar_judgment(
        _quote(price=99, change=1.0, flow=250, avg=95, quadrant="真攻擊"),
    )

    assert got["status"] == "尚未觸發"
    assert "站上關鍵價 100" in got["wait_for"]
    assert "主動買盤持續為正" in got["next_step"]


def test_radar_ui_keeps_factor_sentence_and_exposes_wait_details():
    html = (ROOT / "intraday_decision_dataflow.html").read_text(encoding="utf-8")

    assert "價格位置" in html
    assert "健康換手" in html
    assert "疑似誘高" in html
    assert "七因子：" in html
    assert "等訊號：" in html
    assert "可進場／等回測／保留觀察／尚未觸發／不進場" in html
