# -*- coding: utf-8 -*-
"""
test_filter.py — 篩選公式逐項驗算

執行：python -m pytest tests/ -v    或    python tests/test_filter.py
每個公式單獨 assert，對齊「健康分可逐項驗算」慣例。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.intraday_filter import (
    aflow_from_sides, aflow_official, aflow_ticktype, aflow_reconcile,
    dist_ma20, volume_ratio, proxy_quadrant,
    cond_aflow_positive, cond_above_ma20, cond_quadrant_attack,
    passes_filters, StockSnap,
    engine_signal, engine_stop, attack_signal, attack_stop, next_state,
)


# ---- aflow 兩種算法 ----
def test_aflow_official():
    # Canonical: bid-side=主動買、ask-side=主動賣；正值代表主動買較多。
    assert aflow_from_sides(500, 300) == 200
    assert aflow_from_sides(100, 400) == -300
    # 舊 8000 相容入口維持 aflow_official(sell, buy)。
    assert aflow_official(300, 500) == 200
    assert aflow_official(400, 100) == -300

def test_aflow_ticktype():
    # Shioaji tick_type=1 ask(主動買), 2 bid(主動賣), 0 不計
    stream = [(1, 100), (2, 40), (1, 30), (0, 999)]
    assert aflow_ticktype(stream) == 90

def test_aflow_reconcile_match():
    r = aflow_reconcile(200, 198)
    assert r["diverged"] is False               # 差 2，容忍內

def test_aflow_reconcile_diverge():
    r = aflow_reconcile(200, 120)               # 差 80 → 疑訂閱異常
    assert r["diverged"] is True


# ---- 衍生欄位 ----
def test_dist_ma20():
    assert dist_ma20(142.5, 139.6) == 2.08
    assert dist_ma20(100, None) is None

def test_volume_ratio():
    assert volume_ratio(1840, 1000) == 1.84
    assert volume_ratio(100, 0) is None


# ---- 代理象限四格 ----
def test_quadrant_all_four():
    assert proxy_quadrant(50, 1.2) == "真攻擊"   # 流入+漲
    assert proxy_quadrant(-50, 1.2) == "假紅"    # 流出+漲
    assert proxy_quadrant(50, -1.2) == "惜售"    # 流入+跌（低接）
    assert proxy_quadrant(-50, -1.2) == "休息"   # 流出+跌


# ---- 6/17 被動元件正例：開盤壓吸 = 吸籌，非派發 ----
def test_617_passive_components_case():
    """
    9:05 開盤壓吸階段：主動差顯示流出、價微跌。
    代理象限此刻會是「休息」，但這是洗浮額不是派發——
    公式如實反映代理值，鐵律靠 UI 標「代理·未定案」+ 人工判讀，
    公式本身不該自作聰明把流出改判成流入。
    """
    q_open = proxy_quadrant(aflow=-171, change_rate=-0.11)   # 9:05 壓吸
    assert q_open == "休息"                                   # 代理如實顯示
    q_pull = proxy_quadrant(aflow=120, change_rate=1.89)      # 9:30 V 轉拉起
    assert q_pull == "真攻擊"                                 # 收斂到真攻擊


# ---- 篩選條件 ----
def test_conditions():
    assert cond_aflow_positive(10) is True
    assert cond_aflow_positive(-1) is False
    assert cond_above_ma20(142.5, 139.6) is True
    assert cond_above_ma20(138.0, 139.6) is False
    assert cond_quadrant_attack(50, 1.2) is True
    assert cond_quadrant_attack(-50, 1.2) is False

def test_passes_filters_all_pass():
    s = StockSnap(code="2492", track="engine", price=142.5,
                  change_rate=3.26, aflow=86, total_volume=400,
                  ma20=139.6, inst_buy_days=3)
    r = passes_filters(s)
    assert r["all_pass"] is True

def test_passes_filters_fail():
    # 奇力新：主動差 -6.8、象限假紅 → 不符
    s = StockSnap(code="2456", track="attack", price=88.7,
                  change_rate=0.91, aflow=-7, ma20=90.0)
    r = passes_filters(s)
    assert r["aflow_positive"] is False
    assert r["all_pass"] is False


# ---- 雙軌狀態機（華邦電教訓：軌道不可混）----
def test_engine_track():
    s = StockSnap(code="2492", track="engine", price=142.5,
                  change_rate=3.26, aflow=86, ma20=139.6, inst_buy_days=3)
    assert engine_signal(s) is True             # 站上月線+法人連買
    assert engine_stop(s) is False
    assert next_state("觀察中", s) == "訊號成立"

def test_engine_stop_breaks_ma20():
    s = StockSnap(code="2492", track="engine", price=137.0,
                  change_rate=-1.5, aflow=-20, ma20=139.6, inst_buy_days=3)
    assert engine_stop(s) is True               # 跌破月線
    assert next_state("已進場", s) == "停損觸發"

def test_attack_track():
    s = StockSnap(code="2456", track="attack", price=90.5,
                  change_rate=2.1, aflow=15, trigger_price=90.2, atr_stop=86.1)
    assert attack_signal(s) is True             # 突破觸發價
    assert next_state("觀察中", s) == "訊號成立"

def test_attack_stop_breaks_atr():
    s = StockSnap(code="2456", track="attack", price=85.5,
                  change_rate=-3.0, aflow=-30, trigger_price=90.2, atr_stop=86.1)
    assert attack_stop(s) is True               # 跌破 ATR
    assert next_state("已進場", s) == "停損觸發"

def test_track_isolation():
    """攻擊軌不套引擎軌邏輯：攻擊軌價站上 ma20 但沒突破觸發價 → 不成立。"""
    s = StockSnap(code="2456", track="attack", price=88.0,
                  change_rate=0.5, aflow=5, ma20=87.0, trigger_price=90.2,
                  atr_stop=86.1, inst_buy_days=3)
    # 引擎軌條件成立，但這是攻擊軌 → 只看突破觸發價
    assert attack_signal(s) is False
    assert next_state("觀察中", s) == "觀察中"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); passed += 1; print(f"  PASS  {fn.__name__}")
        except AssertionError:
            print(f"  FAIL  {fn.__name__}"); traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
