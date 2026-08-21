# -*- coding: utf-8 -*-
"""NEXORA 個股盤後報告：資料欄位與風險標示的回歸測試。"""
from pathlib import Path
import sys


MODULE_DIR = Path(__file__).resolve().parent.parent / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(MODULE_DIR))

import extras  # noqa: E402
import money_health  # noqa: E402
import money_health_api as mh  # noqa: E402


def test_post_market_card_does_not_substitute_close_for_missing_vwap(monkeypatch):
    """日 K 沒有 VWAP 時，缺值必須保留，不能把 close 偽裝成 VWAP。"""
    captured = {}

    def capture_stock_health(snapshot, **_kwargs):
        captured.update(snapshot)
        return {"health_score": 50}

    monkeypatch.setattr(money_health, "stock_health", capture_stock_health)
    extras._health_for_card("2464", {"price": 181.5, "avg_price": None}, [])

    assert captured["avg_price"] is None


def test_ma20_risk_uses_ma20_not_ma60_when_labelled_ma20_break():
    """站上 MA20 時不得出現「跌破 MA20」風險，即使仍低於 MA60。"""
    risk = mh.risk_flags(
        {"price": 181.5, "change_rate": 1.97, "volume_ratio": 0.92},
        {"ma20_val": 163.85, "ma60_val": 190.0, "prev_high": 208.0,
         "breakout": 10, "bias_pct": 10.77},
        {},
        {"technical": "ok", "capital": "ok", "chip": "ok", "sector": "ok"},
    )

    assert risk["ma_break"] == 0


def test_near_limit_requires_close_near_limit_not_volume_spike():
    """量比爆量不得被標成「收盤接近漲停」。"""
    risk = mh.risk_flags(
        {"price": 181.5, "prev_close": 178.0, "change_rate": 1.97,
         "volume_ratio": 3.0},
        {"ma20_val": 163.85, "ma60_val": 164.6, "prev_high": 208.0,
         "breakout": 10, "bias_pct": 10.77},
        {},
        {"technical": "ok", "capital": "ok", "chip": "ok", "sector": "ok"},
    )

    assert risk["near_limit"] == 0


def test_breakout_threshold_is_recent_ten_day_high_not_older_history():
    """10 日突破確認只能比較前 10 個交易日，不得吸收更早的高點。"""
    bars = []
    for day in range(21):
        high = 150.0
        if day == 0:
            high = 250.0  # 20 日前，不能成為 10 日前高
        if day == 15:
            high = 208.0  # 前 5 日，應為比較門檻
        bars.append({"date": f"2026-08-{day + 1:02d}", "close": 181.5,
                     "high": high, "low": 170.0, "volume": 1000.0})

    _score, evidence, _quality = mh.score_technical(
        {"price": 181.5, "change_rate": 1.97, "volume_ratio": 0.92}, bars
    )

    assert evidence["prev_high"] == 208.0
