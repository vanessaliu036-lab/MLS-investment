# -*- coding: utf-8 -*-
"""layered_score 單測 —— 鎖住「誤刪率優化規格 §11」驗收清單。

純函式、無副作用、不需 DB。驗收核心:
  1. 低延續分數不單獨觸發淘汰(只降級)
  2. 高追價風險 → 禁追,不移除
  3. 籌碼 Pending 不加分不扣分
  4. 漲停走獨立模型,不因無籌碼被判淘汰
  5. 只有 >=2 項結構失效才淘汰
  6. 雙分數(延續/追價)並存且獨立
  7. 四層齊全(核心/禁追/候補/淘汰)
"""
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "篩選邏輯"
sys.path.insert(0, str(DIR))

import layered_score as L  # noqa: E402


def _bar(**kw):
    b = {"open": 100, "high": 100, "low": 100, "close": 100,
         "ma5": 100, "ma20": 100, "ma60": 100, "volume": 1000, "vol_ma20": 1000}
    b.update(kw)
    return b


# ── §6 漲停獨立模型:漲停不因無盤後籌碼被判淘汰 ──────────────
def test_limit_up_no_chase_not_rejected():
    f = L.build_input("T", _bar(open=100, high=110, low=100, close=110,
                                ma5=101, ma20=95, ma60=90, volume=3500, vol_ma20=1000),
                      None, change_rate=9.8)   # 漲停、無 inst(pending)
    r = L.score_layered(f)
    assert r["tier"] == L.TIER_NO_CHASE           # 禁追,不是淘汰
    assert r["chip_status"] == L.CHIP_PENDING      # 籌碼待補
    assert r["limit_up"] and r["limit_up"]["is_limit_up"]


# ── §5 只有 >=2 項結構失效才淘汰 ─────────────────────────────
def test_two_structural_failures_rejected():
    # V2:需 >=2「不同類」失效。跌破月線(價格) + 法人連續賣超(籌碼,streak<=-2) = 2 類 → 淘汰
    f = L.build_input("T", _bar(open=100, high=101, low=96, close=96,
                                ma5=100, ma20=98, ma60=97, volume=1200, vol_ma20=1000),
                      {"foreign_net": -500, "trust_net": -100, "total_net": -600,
                       "consecutive_days": -3}, change_rate=-2.0)
    r = L.score_layered(f)
    assert r["tier"] == L.TIER_REJECTED
    assert len(L.failure_categories(r["structural_failures"])) >= 2


def test_single_day_selling_not_double_counted():
    # V2 治重複計分:跌破月線 + 單日法人賣超收黑(streak=-1)= 只算「價格」1 類 → 不得淘汰。
    # (舊邏輯會把「法人賣超且收黑」的收黑再算一次,單根黑 K 湊 2 項誤刪。)
    f = L.build_input("T", _bar(open=100, high=101, low=96, close=96,
                                ma5=100, ma20=98, ma60=97, volume=1200, vol_ma20=1000),
                      {"foreign_net": -500, "trust_net": -100, "total_net": -600,
                       "consecutive_days": -1}, change_rate=-2.0)
    r = L.score_layered(f)
    assert r["tier"] != L.TIER_REJECTED
    assert len(L.failure_categories(r["structural_failures"])) < 2


def test_single_failure_not_rejected():
    # 只跌破5MA(close<ma5),但仍在月線上、法人買、收紅 → 只有 1 項 → 不得淘汰
    f = L.build_input("T", _bar(open=99, high=100, low=98, close=99,
                                ma5=100, ma20=96, ma60=94, volume=1100, vol_ma20=1000),
                      {"foreign_net": 300, "trust_net": 100, "total_net": 400,
                       "consecutive_days": 1}, change_rate=0.5)
    r = L.score_layered(f)
    assert r["tier"] != L.TIER_REJECTED
    assert len(r["structural_failures"]) < 2


# ── §1 低延續分數不單獨淘汰(只降級為候補)────────────────────
def test_low_continuation_not_deleted():
    # 資料稀薄→延續分數低,但無結構失效、非高乖離 → 候補,不刪
    f = L.build_input("T", _bar(close=100, ma5=100, ma20=99, ma60=98,
                                volume=1000, vol_ma20=1000), None, change_rate=0.0)
    r = L.score_layered(f)
    assert r["tier"] in (L.TIER_CANDIDATE, L.TIER_CORE)
    assert r["tier"] != L.TIER_REJECTED


# ── §6 籌碼 Pending 不加分不扣分 ─────────────────────────────
def test_chip_pending_no_penalty():
    f = L.build_input("T", _bar(), None)   # 無 inst
    r = L.score_layered(f)
    assert r["chip_status"] == L.CHIP_PENDING
    # 資金持續性/籌碼同步應列 pending(不列入分母),而非以 0 分拖累
    assert "資金持續性" in r["pending"]
    assert "籌碼同步" in r["pending"]


def test_chip_negative_vs_positive():
    pos = L.score_layered(L.build_input("T", _bar(),
          {"foreign_net": 500, "trust_net": 200, "total_net": 700, "consecutive_days": 3}))
    neg = L.score_layered(L.build_input("T", _bar(),
          {"foreign_net": -500, "trust_net": -200, "total_net": -700, "consecutive_days": -3}))
    assert pos["chip_status"] == L.CHIP_POSITIVE
    assert neg["chip_status"] == L.CHIP_NEGATIVE
    assert pos["continuation"] > neg["continuation"]   # 買超延續分應高於賣超


# ── §4.2 高乖離(距5MA>=7%)→ 類別性禁追,不被收盤穩稀釋 ──────
def test_high_bias_forces_no_chase():
    # 收最高(上影0、收盤位置1)本會拉低追價風險,但高乖離閘仍須判禁追
    f = L.build_input("T", _bar(open=100, high=110, low=100, close=110,
                                ma5=100, ma20=95, ma60=90, volume=1500, vol_ma20=1000),
                      {"foreign_net": 100, "trust_net": 50, "total_net": 150,
                       "consecutive_days": 1}, change_rate=8.0)
    r = L.score_layered(f)
    assert r["tier"] == L.TIER_NO_CHASE


# ── §3 雙分數並存且獨立 ─────────────────────────────────────
def test_dual_scores_independent():
    r = L.score_layered(L.build_input("T", _bar(open=100, high=108, low=100, close=108,
                        ma5=101, ma20=96, ma60=92, volume=1600, vol_ma20=1000),
                        {"foreign_net": 400, "trust_net": 200, "total_net": 600,
                         "consecutive_days": 2}, change_rate=6.5))
    assert "continuation" in r and "chase_risk" in r
    assert "chase_safety" in r
    assert 0 <= r["continuation"] <= 100 and 0 <= r["chase_risk"] <= 100
    # 強勢+乖離 → 追價風險應明顯 > 0,兩分數不相等
    assert r["continuation"] != r["chase_risk"]


# ── §7 四層都到得了 ─────────────────────────────────────────
def test_all_four_tiers_reachable():
    tiers = set()
    # 核心:健康、低乖離、法人買
    tiers.add(L.score_layered(L.build_input("T", _bar(open=100, high=106, low=100, close=106,
        ma5=101, ma20=96, ma60=92, volume=1500, vol_ma20=1000),
        {"foreign_net": 500, "trust_net": 300, "total_net": 800, "consecutive_days": 3},
        change_rate=3.0))["tier"])
    # 禁追:高乖離
    tiers.add(L.score_layered(L.build_input("T", _bar(open=100, high=110, low=100, close=110,
        ma5=100, ma20=95, ma60=90, volume=1500, vol_ma20=1000), None, change_rate=8.0))["tier"])
    # 候補:結構尚可(站上均線、無失效)但趨勢弱且資料薄 → 延續<65、非高乖離
    tiers.add(L.score_layered(L.build_input("T", _bar(open=101, high=103, low=101, close=102,
        ma5=100, ma20=101, ma60=103, volume=1000, vol_ma20=1000), None,
        change_rate=2.0))["tier"])
    # 淘汰:多重結構失效
    tiers.add(L.score_layered(L.build_input("T", _bar(open=100, high=101, low=96, close=96,
        ma5=100, ma20=98, ma60=97, volume=1200, vol_ma20=1000),
        {"foreign_net": -500, "trust_net": -100, "total_net": -600, "consecutive_days": -2},
        change_rate=-2.0))["tier"])
    assert tiers == {L.TIER_CORE, L.TIER_NO_CHASE, L.TIER_CANDIDATE, L.TIER_REJECTED}
