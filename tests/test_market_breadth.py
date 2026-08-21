# -*- coding: utf-8 -*-
"""market_breadth 逐項驗算 — 作戰模式門檻、真假行情、時間序列。

執行：python -m pytest tests/test_market_breadth.py -v
純 SQLite，不打任何外部 API。
"""

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_breadth as M  # noqa: E402


def setup_function():
    M.DB_PATH = Path(tempfile.mkdtemp()) / "breadth_test.db"
    # 本檔要測的是「market_regime 不可用時純 aflow 代理/門檻邏輯」(docstring:
    # 純 SQLite,不打外部 API)。但若同一 pytest 進程先跑過別的測試檔
    # (例如 test_after_hours_model.py 把「個股卡片相關檔案_20260722」塞進
    # sys.path 並匯入過 market_regime)，Python 模組快取(sys.modules)會讓
    # 這裡的 compute() 意外接到「真的」market_regime，連到真實資料庫，
    # 回傳跟假造 rows 完全無關的今日真實數字(2026-08-21 抓到：
    # assert 65.9 == 70.6，65.9 正是當下 VPS 大盤寬度真實值，不是本檔任何
    # 一組假資料能產生的數字)。每次測試前把它從快取清掉，逼 compute() 走
    # 本檔要測的那條路徑，不看其他檔案的執行順序臉色。
    # pop 不夠：其他測試檔可能把「個股卡片相關檔案_20260722」也留在 sys.path 上，
    # 光清模組快取，import market_regime 還是會重新從那個目錄挖到真的檔案。
    # 塞 None 是 Python import 系統認得的哨兵值，會讓 import 保證失敗，不管
    # sys.path 上還留著什麼路徑。
    sys.modules["market_regime"] = None


def teardown_function():
    sys.modules.pop("market_regime", None)


def rows(n_in, n_out, n_missing=0):
    return ([{"code": f"i{i}", "aflow": 100} for i in range(n_in)]
            + [{"code": f"o{i}", "aflow": -100} for i in range(n_out)]
            + [{"code": f"x{i}", "aflow": None} for i in range(n_missing)])


def at(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=M.TW_TZ)


def test_risk_on_門檻_真實寬度可判():
    """真實寬度不可得(降級 aflow 代理)時，「鐵律」封頂中性，不得判 Risk On——
    崩盤日 aflow 也會系統性偏正，代理值不可信到能升格積極操作(見 compute() 內
    「鐵律：aflow 代理值不得升格 Risk On」)。本測試原本直接斷言 70.6%→risk_on，
    寫在鐵律加上之前，跟現在的程式碼(已是正式站在跑的版本)矛盾，2026-08-21 對齊。
    """
    b = M.compute(rows(36, 15), index_pct=1.0)      # 36/51 = 70.6%
    assert b["ratio_pct"] == 70.6
    assert b["level"] == "neutral"
    assert "代理" in b["level_advice"]


def test_risk_off_門檻():
    b = M.compute(rows(13, 38), index_pct=0.2)      # 25.5%
    assert b["level"] == "risk_off"


def test_窄幅行情_指數漲但資金沒跟():
    """加權指數 +2%，51 檔只有 13 檔 aflow>0 → 假行情，必須點名。"""
    b = M.compute(rows(13, 38), index_pct=2.0)
    assert b["state"] == "narrow"
    assert "Narrow Breadth" in b["state_title"]
    assert "權值股" in b["state_note"]


def test_全面性上攻_指數與資金同步():
    b = M.compute(rows(36, 15), index_pct=2.0)
    assert b["state"] == "broad"


def test_指數未知_高廣度仍判資金全面參與():
    """加權指數暫無資料時，94% 這種高廣度必須給單一真實結論，不留「指數未知」。"""
    b = M.compute(rows(48, 3), index_pct=None)      # 94.1%
    assert b["state"] == "broad_nomkt"
    assert b["state_title"] == "資金全面參與"
    assert "94% 資金流入" in b["state_note"]


def test_指數未知_低廣度判資金偏窄():
    b = M.compute(rows(13, 38), index_pct=None)      # 25.5%
    assert b["state"] == "narrow_nomkt" and "窄幅" in b["state_note"]


def test_指數未知_中段廣度判中性():
    b = M.compute(rows(26, 25), index_pct=None)      # 51%
    assert b["state"] == "neutral_nomkt"


def test_指數殺低但資金留守():
    b = M.compute(rows(30, 21), index_pct=-1.5)     # 58.8%
    assert b["state"] == "resilient"


def test_缺aflow不計入分母():
    b = M.compute(rows(10, 10, n_missing=31), index_pct=0.0)
    assert b["total"] == 20 and b["ratio_pct"] == 50.0


def test_樣本不足不誤判riskon():
    """服務剛重啟只有 1 檔回報時，1 檔流入不可判成 100% Risk On。"""
    b = M.compute(rows(1, 0), index_pct=0.0)
    assert b["ratio_pct"] == 100.0 and b["thin_sample"] and b["no_data"]
    assert M.record(b, now=at("2026-07-23T10:00:00")) is False


def test_時間序列_連續擴散天數():
    for day, n_in in [("2026-07-20", 20), ("2026-07-21", 25),
                      ("2026-07-22", 30), ("2026-07-23", 36)]:
        now = at(f"{day}T10:00:00")
        M.record(M.compute(rows(n_in, 51 - n_in), index_pct=1.0), now=now)
    t = M.trend(M.history(10))
    assert t["direction"] == "up" and t["streak"] == 3
    assert "每天都有更多股票加入資金流入" in t["note"]


def test_時間序列_收縮也要抓到():
    for day, n_in in [("2026-07-20", 36), ("2026-07-21", 30), ("2026-07-22", 20)]:
        M.record(M.compute(rows(n_in, 51 - n_in), index_pct=-0.5),
                 now=at(f"{day}T10:00:00"))
    t = M.trend(M.history(10))
    assert t["direction"] == "down" and t["streak"] == 2


def test_盤後不再開新紀錄():
    assert M.record(M.compute(rows(30, 21)), now=at("2026-07-23T10:00:00")) is True
    assert M.record(M.compute(rows(30, 21)), now=at("2026-07-23T20:00:00")) is False


def test_日內曲線_五分鐘一點():
    for hhmm in ["10:00:10", "10:02:00", "10:07:00"]:
        M.record(M.compute(rows(30, 21)), now=at(f"2026-07-23T{hhmm}"))
    curve = M.today_curve("2026-07-23")
    assert [c["hhmm"] for c in curve] == ["10:00", "10:05"]


def test_今日無資料回退最近一日_不留空白():
    M.record(M.compute(rows(36, 15), index_pct=1.0), now=at("2026-07-23T13:00:00"))
    p = M.api_payload(rows=[], now=at("2026-07-24T08:00:00"))
    assert p["stale"] is True and p["data_date"] == "2026-07-23"
    assert p["ratio_pct"] == 70.6 and p["notes"]


def test_api_payload_今日即時值蓋過已落地的舊值():
    M.record(M.compute(rows(20, 31), index_pct=1.0), now=at("2026-07-23T10:00:00"))
    p = M.api_payload(rows=rows(36, 15), index_pct=1.0, now=at("2026-07-23T11:00:00"))
    assert p["stale"] is False and p["ratio_pct"] == 70.6
    assert p["series"][-1]["ratio_pct"] == 70.6


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
