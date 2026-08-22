# -*- coding: utf-8 -*-
"""NEXORA 個股盤後報告：資料欄位與風險標示的回歸測試。"""
from pathlib import Path
import json
import sqlite3
import sys
import types


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


def test_near_limit_uses_previous_close_from_technical_evidence_when_snapshot_lacks_it():
    """盤後快照沒有昨收時，仍要用同一組日 K 的昨收判斷收盤是否接近漲停。"""
    risk = mh.risk_flags(
        {"price": 195.5, "change_rate": 9.83, "volume_ratio": 1.0},
        {"ma20_val": 163.85, "ma60_val": 164.6, "prev_close": 178.0,
         "prev_high": 208.0, "breakout": 20, "bias_pct": 19.4},
        {},
        {"technical": "ok", "capital": "ok", "chip": "ok", "sector": "ok"},
    )

    assert risk["near_limit"] == 1


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


def test_missing_net_active_is_excluded_from_score_and_lowers_confidence_only():
    """主動資金缺值只能降低可信度，不能在 100 分分母中當成 0 分。"""
    decision = extras._decision_factors(
        {
            "health_score": 100,
            "chip": {
                "foreign_net_20d": 19398,
                "margin_change_5d": -3124,
                "inst_streak": 4,
                "big400_delta": 1,
            },
            "flow": {"active_buy_pct": 51},
        },
        {"price": 181.5, "ma20": 163.85, "aflow": None},
    )

    assert decision["score"] == 97.4
    assert decision["score_available"] == 78
    assert decision["confidence"] == "Medium"


def test_sector_members_are_exposed_for_auditable_equal_weight_average():
    """族群均值必須同時回傳實際採用的固定池成分。"""
    members = extras._sector_members("無人機")

    assert [member["code"] for member in members] == ["2049", "2359", "2464", "4919"]
    assert all(member["name"] for member in members)


def test_individual_detail_ui_labels_institution_and_margin_periods_explicitly():
    """個股詳情不得把 5D 欄位呈現成當日資料。"""
    html = (Path(__file__).resolve().parent.parent / "intraday_decision_dataflow.html").read_text(
        encoding="utf-8"
    )

    assert "外資 Today" in html
    assert "外資 5D" in html
    assert "融資 5D" in html
    assert "個股決策分" in html


def test_post_market_card_reads_persisted_vwap_instead_of_close(tmp_path):
    """收盤個股卡要取同交易日落地的 VWAP，而非日 K 的收盤價。"""
    db_path = tmp_path / "intraday_eod.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE intraday_stock_daily "
            "(trade_date TEXT, code TEXT, avg_price REAL, high REAL, low REAL, "
            "aflow REAL, volume INTEGER, volume_ratio REAL)"
        )
        conn.execute(
            "INSERT INTO intraday_stock_daily VALUES "
            "('2026-08-19', '2464', 182.99, 190, 172, NULL, 1000, 0.92)"
        )

    daily = extras._read_intraday_daily("2464", "2026-08-19", db_path)

    assert daily["avg_price"] == 182.99


def test_old_card_cache_is_not_served_after_data_contract_changes(tmp_path, monkeypatch):
    """不含資料契約版本的舊卡片快取必須失效，不能覆蓋新欄位。"""
    monkeypatch.setattr(extras, "_CARD_DIR", tmp_path)
    monkeypatch.setattr(extras, "_CARD_MEM", {})
    (tmp_path / "2026-08-19_2464.json").write_text(
        json.dumps({"ok": True, "card": {"decision": {"rule": "old"}}}), encoding="utf-8"
    )

    assert extras._card_cache_read("2464", "2026-08-19") is None


def test_intraday_quote_replaces_stale_eod_price_and_change(monkeypatch):
    """盤中卡片不得把前一日收盤跌幅當成今日現價漲跌。"""
    monkeypatch.setattr(extras, "_is_intraday_session", lambda: True)
    monkeypatch.setattr(extras.broker, "buffer_snapshots", lambda _codes: [{
        "code": "3532", "price": 347.5, "change_rate": 2.21,
        "high": 350.0, "low": 338.0, "avg_price": 344.9,
        "volume_ratio": 1.08, "total_volume": 12345,
    }])

    merged = extras._merge_intraday_quote("3532", {
        "price": 340.0, "change_rate": -4.63,
        "prev_close": 356.5, "source_date": "2026-08-20",
        "data_mode": "post_market_daily_kbar",
    })

    assert merged["price"] == 347.5
    assert merged["change_rate"] == 2.21
    assert merged["eod_close"] == 340.0
    assert merged["data_mode"] == "intraday_shioaji"
    assert merged["price_source"] == "Shioaji 即時推播"


def test_report_daily_bars_fall_back_to_official_source_and_never_use_future_bars(tmp_path, monkeypatch):
    """歷史報告沒有 DB 日 K 時，取官方日 K 並截在報告日，不得偷看未來。"""
    monkeypatch.setattr(mh, "DB_PATH", tmp_path / "empty.db")
    monkeypatch.setitem(sys.modules, "eod_source", types.SimpleNamespace(
        _price_rows=lambda _code, _start_date, trade_date=None: [
            {"date": "2026-08-17", "close": 200, "max": 208, "min": 190,
             "Trading_Volume": 1000},
            {"date": "2026-08-19", "close": 181.5, "max": 190, "min": 172,
             "Trading_Volume": 1200},
            {"date": "2026-08-20", "close": 177, "max": 189, "min": 176,
             "Trading_Volume": 900},
        ]
    ))

    bars = mh._read_daily_bars("2464", days=70, asof="2026-08-19")

    assert [bar["date"] for bar in bars] == ["2026-08-17", "2026-08-19"]
    assert max(bar["high"] for bar in bars) == 208


def test_short_official_history_uses_full_history_fallback_for_ma20(tmp_path, monkeypatch):
    """官方備援不足 20 根時，必須改用完整歷史來源，不能產生空 MA20。"""
    monkeypatch.setattr(mh, "DB_PATH", tmp_path / "empty.db")
    monkeypatch.setitem(sys.modules, "eod_source", types.SimpleNamespace(
        _price_rows=lambda *_args, **_kwargs: [
            {"date": f"2026-08-{day:02d}", "close": 180, "max": 181, "min": 179,
             "Trading_Volume": 1000}
            for day in range(1, 14)
        ]
    ))
    full = [
        {"date": f"2026-07-{day:02d}", "close": 160, "high": 161, "low": 159,
         "volume": 1000}
        for day in range(1, 21)
    ]
    monkeypatch.setattr(mh, "_yahoo_daily_bars", lambda *_args, **_kwargs: full)

    bars = mh._read_daily_bars("2464", days=70, asof="2026-08-19")

    assert bars == full
