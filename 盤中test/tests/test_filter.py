# -*- coding: utf-8 -*-
"""test_filter.py — v4 全驗算：三態 + MA20 + 極端價防護"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.intraday_filter import (
    aflow_official, aflow_ticktype, aflow_reconcile,
    dist_ma20, volume_ratio, aflow_intensity, proxy_quadrant,
    is_extreme_price, EXTREME_PCT, AFLOW_INTENSITY_MIN,
    PASS, FAIL, NO_DATA,
    st_aflow_positive, st_above_ma20, st_aflow_intensity, st_regime_quadrant,
    cond_aflow_positive, cond_above_ma20, cond_aflow_intensity,
    cond_quadrant_attack, cond_strong_absorb,
    passes_filters, rank_potential, StockSnap,
    market_regime, REGIME_ATTACK, REGIME_DEFENSE, REGIME_RANGE,
    engine_signal, engine_stop, attack_signal, attack_stop, next_state,
)
from app.prefetch_ma20 import compute_ma20, build_ma20_cache_from_closes


def snap(**kw):
    base = dict(code="0000", track="engine", price=100.0, change_rate=0.0,
                aflow=0, total_volume=0, ma20=None,
                trigger_price=None, atr_stop=None, inst_buy_days=0)
    base.update(kw)
    return StockSnap(**base)

# ---- aflow ----
def test_aflow_official():
    assert aflow_official(500, 300) == 200
    assert aflow_official(100, 400) == -300

def test_aflow_ticktype():
    assert aflow_ticktype([(1,100),(2,40),(1,30),(0,999)]) == 90

def test_aflow_reconcile():
    assert aflow_reconcile(200, 198)["diverged"] is False
    assert aflow_reconcile(200, 120)["diverged"] is True

# ---- 衍生 ----
def test_dist_ma20():
    assert dist_ma20(142.5, 139.6) == 2.08
    assert dist_ma20(100, None) is None

def test_volume_ratio():
    assert volume_ratio(1840, 1000) == 1.84
    assert volume_ratio(100, 0) is None

def test_aflow_intensity():
    assert aflow_intensity(136, 368) == 37.0
    assert aflow_intensity(97, 2237) == 4.3
    assert aflow_intensity(100, 0) is None

def test_quadrant():
    assert proxy_quadrant(50, 1.2) == "真攻擊"
    assert proxy_quadrant(-50, 1.2) == "假紅"
    assert proxy_quadrant(50, -1.2) == "惜售"
    assert proxy_quadrant(-50, -1.2) == "休息"

# ---- 極端價防護 ----
def test_is_extreme_price():
    assert is_extreme_price(-9.27) is True    # 逼近跌停
    assert is_extreme_price(9.5) is True       # 逼近漲停
    assert is_extreme_price(-3.96) is False
    assert is_extreme_price(-9.0) is True      # 邊界含等於

def test_extreme_forces_no_data():
    """跌停股：aflow 正值但失真，主動差/吸籌/象限全判 NO_DATA，不進符合清單。"""
    # 世界 5347 型：-9.72%、aflow +164359、量 264531
    s = snap(code="5347", price=130, change_rate=-9.72,
             aflow=164359, total_volume=264531, ma20=140.0)
    assert st_aflow_positive(s) == NO_DATA
    assert st_aflow_intensity(s) == NO_DATA
    assert st_regime_quadrant(s, REGIME_DEFENSE) == NO_DATA
    r = passes_filters(s, regime=REGIME_DEFENSE)
    assert r["all_pass"] is False              # 有 NO_DATA 絕不 all_pass
    assert r["extreme"] is True
    assert "主動差>0" in r["no_data"]
    assert "\u2014主動差>0" in r["display"]      # 顯示 —（NO_DATA）不是 ✗

# ---- 三態：MA20 未接入 = NO_DATA 不是 FAIL ----
def test_ma20_not_loaded_is_no_data():
    s = snap(code="2492", price=142.5, change_rate=3.26, aflow=86,
             total_volume=400, ma20=None)   # MA20 未接入
    assert st_above_ma20(s) == NO_DATA
    r = passes_filters(s)
    assert "站上MA20" in r["no_data"]         # 在 no_data，不在 failed
    assert "站上MA20" not in r["failed"]
    assert "\u2014站上MA20" in r["display"]     # 顯示 — 不是 ✗
    assert r["all_pass"] is False             # 有 NO_DATA 不算全過

def test_ma20_loaded_pass_fail():
    assert st_above_ma20(snap(price=142.5, ma20=139.6)) == PASS
    assert st_above_ma20(snap(price=138.0, ma20=139.6)) == FAIL

# ---- 反相 bug 回歸：負 aflow 絕不在 passed ----
def test_negative_aflow_not_passed():
    s = snap(code="3532", price=442.5, change_rate=4.36, aflow=-70,
             total_volume=8472, ma20=400.0)
    r = passes_filters(s)
    assert "主動差>0" in r["failed"]
    assert "主動差>0" not in r["passed"]

# ---- all_pass 需無 NO_DATA 且全 PASS ----
def test_all_pass_requires_no_nodata():
    s = snap(code="2492", price=142.5, change_rate=3.26, aflow=86,
             total_volume=400, ma20=139.6, inst_buy_days=3)
    r = passes_filters(s, regime=REGIME_ATTACK)
    assert r["all_pass"] is True
    assert r["no_data"] == []

# ---- 盤勢 ----
def test_market_regime():
    assert market_regime(68) == REGIME_ATTACK
    assert market_regime(30) == REGIME_DEFENSE
    assert market_regime(50) == REGIME_RANGE

def test_strong_absorb_regime():
    # 昇陽型（非極端價）：-3.96% 強惜售
    s = snap(code="8028", price=302.5, change_rate=-3.96, aflow=1077,
             total_volume=7203, ma20=300.0)
    assert st_regime_quadrant(s, REGIME_DEFENSE) == PASS
    assert st_regime_quadrant(s, REGIME_ATTACK) == FAIL
    r = passes_filters(s, regime=REGIME_DEFENSE)
    assert r["all_pass"] is True
    assert "強惜售承接" in r["passed"]

# ---- MA20 計算 ----
def test_compute_ma20():
    closes = [float(i) for i in range(1, 21)]   # 1..20 → 均 10.5
    assert compute_ma20(closes) == 10.5
    assert compute_ma20([1,2,3]) is None         # 不足 20 → None 不補造

def test_ma20_cache_from_closes():
    cache = build_ma20_cache_from_closes({
        "2492": [float(i) for i in range(1, 21)],
        "NEW":  [1.0, 2.0],                       # 資料不足
    })
    assert cache["2492"] == 10.5
    assert cache["NEW"] is None

def test_ma20_cache_feeds_filter():
    """MA20 快取接入後，st_above_ma20 從 NO_DATA 變 PASS/FAIL。"""
    cache = build_ma20_cache_from_closes({"2492":[float(i) for i in range(1,21)]})
    s = snap(code="2492", price=12.0, ma20=cache["2492"])   # 12 > 10.5
    assert st_above_ma20(s) == PASS

# ---- rank：極端價排最後 ----
def test_rank_extreme_last():
    normal = snap(code="8028", aflow=1077, total_volume=7203, change_rate=-3.96)
    extreme = snap(code="5347", aflow=164359, total_volume=264531, change_rate=-9.72)
    ranked = rank_potential([extreme, normal])
    assert ranked[0]["code"] == "8028"           # 正常盤在前
    assert ranked[-1]["code"] == "5347"          # 極端價墊底
    assert ranked[-1]["extreme"] is True

# ---- 狀態機 ----
def test_engine_track():
    s = snap(price=142.5, change_rate=3.26, aflow=86, ma20=139.6, inst_buy_days=3)
    assert engine_signal(s) is True
    assert next_state("觀察中", s) == "訊號成立"

def test_engine_ma20_missing_no_false_signal():
    """MA20 未接入時引擎軌不誤發訊號、不誤停損。"""
    s = snap(price=142.5, change_rate=3.26, aflow=86, ma20=None, inst_buy_days=3)
    assert engine_signal(s) is False
    assert engine_stop(s) is False

def test_attack_track():
    s = snap(track="attack", price=90.5, change_rate=2.1, aflow=15,
             trigger_price=90.2, atr_stop=86.1)
    assert attack_signal(s) is True
    assert next_state("觀察中", s) == "訊號成立"
    s2 = snap(track="attack", price=85.5, change_rate=-3.0, aflow=-30,
              trigger_price=90.2, atr_stop=86.1)
    assert attack_stop(s2) is True
    assert next_state("已進場", s2) == "停損觸發"

def test_track_isolation():
    s = snap(track="attack", price=88.0, change_rate=0.5, aflow=5, ma20=87.0,
             trigger_price=90.2, atr_stop=86.1, inst_buy_days=3)
    assert attack_signal(s) is False
    assert next_state("觀察中", s) == "觀察中"



# ================= AI 白話解讀 =================
from app.ai_explain import local_explain, claude_explain
from app.test_page_wiring import make_prefetch_of, build_rows

def test_ai_extreme_warns_off():
    """聯電跌停爆量：AI 必須明講訊號不可信、先別碰。"""
    s = snap(code="2303", price=130, change_rate=-9.72, aflow=178306,
             total_volume=281032, ma20=140.0)
    txt = local_explain(s, regime=REGIME_RANGE)
    assert "跌停" in txt and "別碰" in txt

def test_ai_strong_absorb():
    """欣銓 -7.24% 分點強吸籌但沒法人資料：
    2026-07-20 改：缺法人 → 不能硬蓋「強惜售」標籤（華邦電教訓：先確保數據正確再談判斷）。
    必須講「資料未接入」並提示觀察法人動向。"""
    s = snap(code="3264", price=198.5, change_rate=-7.24, aflow=810,
             total_volume=4952, ma20=210.0)
    txt = local_explain(s, regime=REGIME_RANGE)
    # 沒法人資料 → 不能硬蓋「強惜售」
    assert "強惜售" not in txt, f"缺法人資料不應講「強惜售」：{txt}"
    # 必須提示「資料未接入」並請觀察
    assert "法人" in txt and ("未接入" in txt or "確認" in txt or "觀察" in txt)

def test_ai_ma20_note():
    """MA20 未接入時，AI 附註引擎軌暫無法驗證。"""
    s = snap(code="8182", price=39.5, change_rate=-6.83, aflow=66,
             total_volume=1148, ma20=None)
    txt = local_explain(s, regime=REGIME_RANGE)
    assert "MA20 未接入" in txt

def test_ai_never_empty():
    """任何輸入都必有一行輸出（不開天窗）。"""
    for cr, af in [(0.0,0),(5.0,100),(-9.9,50000),(-2.0,-30)]:
        s = snap(change_rate=cr, aflow=af, total_volume=1000)
        assert len(local_explain(s, regime=REGIME_RANGE)) > 0

def test_claude_explain_falls_back_without_key():
    """無 API key → 退回本地解讀，等同 local。"""
    s = snap(code="3264", price=198.5, change_rate=-7.24, aflow=810,
             total_volume=4952, ma20=210.0)
    assert claude_explain(s, REGIME_RANGE, api_key=None) == local_explain(s, REGIME_RANGE)

def test_wiring_ma20_none_shows_nodata():
    """MA20 快取給 None → 該檔 above_ma20 進 no_data，前端顯示「—」。"""
    cache = {"8182": None, "3264": 210.0}
    pf = make_prefetch_of(cache, {"8182":1000,"3264":5000}, {}, {})
    meta = {"8182": dict(name="加高", price=39.5, change_rate=-6.83, aflow=66,
                         total_volume=1148, track="attack"),
            "3264": dict(name="欣銓", price=198.5, change_rate=-7.24, aflow=810,
                         total_volume=4952, track="attack")}
    rows = build_rows(["8182","3264"], lambda c: meta[c], pf, REGIME_RANGE)
    r8182 = next(r for r in rows if r["code"]=="8182")
    r3264 = next(r for r in rows if r["code"]=="3264")
    assert "站上MA20" in r8182["filter_no_data"]      # None → NO_DATA
    assert "站上MA20" not in r3264["filter_no_data"]  # 有 MA20 → 正常判
    assert all("ai" in r and r["ai"] for r in rows)   # 每檔都有 AI 解讀


# ================= 分類：三大群 + 象限細分 =================
from app.classify import (
    classify_one, classify_all,
    GROUP_ACTIONABLE, GROUP_WATCH, GROUP_EXCLUDE,
    SUB_TRUE_ATTACK, SUB_STRONG_ABSORB, SUB_EXTREME,
)

def test_classify_extreme_excluded():
    """聯電跌停爆量 → 排除/極端價失真。"""
    s = snap(code="2303", price=130, change_rate=-9.72, aflow=178306,
             total_volume=281032, ma20=140.0)
    c = classify_one(s, regime=REGIME_RANGE)
    assert c["group"] == GROUP_EXCLUDE
    assert c["subgroup"] == SUB_EXTREME

def test_classify_strong_absorb_actionable():
    """欣銓強惜售全過 → 可操作/強惜售抄底。"""
    s = snap(code="3264", price=198.5, change_rate=-7.24, aflow=810,
             total_volume=4952, ma20=210.0)
    c = classify_one(s, regime=REGIME_DEFENSE)
    assert c["group"] == GROUP_ACTIONABLE
    assert c["subgroup"] == SUB_STRONG_ABSORB

def test_classify_true_attack_actionable():
    s = snap(code="2492", price=142.5, change_rate=3.26, aflow=86,
             total_volume=400, ma20=139.6)
    c = classify_one(s, regime=REGIME_ATTACK)
    assert c["group"] == GROUP_ACTIONABLE
    assert c["subgroup"] == SUB_TRUE_ATTACK

def test_classify_ma20_missing_to_watch():
    """真攻擊追漲屬引擎軌，MA20 未接入 → 觀察（不能進可操作）。"""
    s = snap(code="2492", price=142.5, change_rate=3.26, aflow=86,
             total_volume=400, ma20=None)          # 真攻擊但 MA20 未接入
    c = classify_one(s, regime=REGIME_ATTACK)
    assert c["group"] == GROUP_WATCH

def test_classify_strong_absorb_ignores_ma20():
    """強惜售抄底屬攻擊軌，不綁 MA20，未接入仍可操作（華邦電教訓：軌道不混）。"""
    s = snap(code="3264", price=198.5, change_rate=-7.24, aflow=810,
             total_volume=4952, ma20=None)
    c = classify_one(s, regime=REGIME_DEFENSE)
    assert c["group"] == GROUP_ACTIONABLE
    assert c["subgroup"] == SUB_STRONG_ABSORB

def test_classify_resting_excluded():
    """華容 休息（跌+流出）→ 排除。"""
    s = snap(code="5328", price=55.6, change_rate=-7.79, aflow=-242,
             total_volume=9334, ma20=60.0)
    c = classify_one(s, regime=REGIME_RANGE)
    assert c["group"] == GROUP_EXCLUDE

def test_classify_all_structure():
    """全池分類巢狀結構 + 計數正確。"""
    snaps = [
        snap(code="3264", price=198.5, change_rate=-7.24, aflow=810,
             total_volume=4952, ma20=210.0),      # 可操作/強惜售
        snap(code="2303", price=130, change_rate=-9.72, aflow=178306,
             total_volume=281032, ma20=140.0),    # 排除/極端
        snap(code="5328", price=55.6, change_rate=-7.79, aflow=-242,
             total_volume=9334, ma20=60.0),       # 排除/休息
    ]
    res = classify_all(snaps, regime=REGIME_DEFENSE)
    assert res["counts"][GROUP_ACTIONABLE] == 1
    assert res["counts"][GROUP_EXCLUDE] == 2
    assert "強惜售抄底" in res[GROUP_ACTIONABLE]


# ================= classify_flat 排序 =================
from app.classify import classify_flat, GROUP_ACTIONABLE, GROUP_WATCH, GROUP_EXCLUDE

def test_classify_flat_group_order():
    """可操作→觀察→排除；群內 aflow 強者在前。"""
    snaps = [
        snap(code="EXC", price=100, change_rate=-9.72, aflow=178306,
             total_volume=281032, ma20=110.0),        # 排除/極端
        snap(code="ACT", price=198.5, change_rate=-7.24, aflow=810,
             total_volume=4952, ma20=None),           # 可操作/強惜售(攻擊軌不綁MA20)
        snap(code="WAT", price=142.5, change_rate=3.26, aflow=1722,
             total_volume=400, ma20=None),            # 觀察(真攻擊但MA20未接入)
    ]
    flat = classify_flat(snaps, regime=REGIME_DEFENSE)
    groups_in_order = [r["group"] for r in flat]
    # 可操作必在觀察前，觀察必在排除前
    assert groups_in_order.index(GROUP_ACTIONABLE) < groups_in_order.index(GROUP_WATCH)
    assert groups_in_order.index(GROUP_WATCH) < groups_in_order.index(GROUP_EXCLUDE)

def test_classify_flat_intra_group_aflow():
    """同群內 aflow 大的在前。"""
    snaps = [
        snap(code="A_small", price=200, change_rate=-7.0, aflow=500,
             total_volume=2000, ma20=None),
        snap(code="A_big", price=200, change_rate=-7.0, aflow=3000,
             total_volume=5000, ma20=None),
    ]
    flat = classify_flat(snaps, regime=REGIME_DEFENSE)
    acts = [r["code"] for r in flat if r["group"] == GROUP_ACTIONABLE]
    assert acts == ["A_big", "A_small"]

# ================= 收盤蓋章 =================
import tempfile, os as _os
from app.eod_stamp import run_eod_stamp, load_eod, ensure_table
import sqlite3 as _sq

def test_eod_stamp_and_load():
    db = tempfile.mktemp(suffix=".db")
    try:
        snaps = [
            snap(code="3264", price=198.5, change_rate=-7.24, aflow=810,
                 total_volume=4952, ma20=210.0),
            snap(code="2303", price=130, change_rate=-9.72, aflow=178306,
                 total_volume=281032, ma20=140.0),   # 極端價
        ]
        res = run_eod_stamp(db, snaps, thermometer_score=30, trade_date="2026-07-20")
        assert res["stamped"] == 2
        rows = load_eod(db, "2026-07-20")
        assert len(rows) == 2
        # 極端價那檔 signal_reliable=0
        r2303 = next(r for r in rows if r["code"]=="2303")
        assert r2303["signal_reliable"] == 0
        assert r2303["data_stage"] == "eod_stamped"
    finally:
        if _os.path.exists(db): _os.remove(db)

def test_eod_no_overwrite_across_days():
    """不同日各自獨立一列，不互相覆蓋。"""
    db = tempfile.mktemp(suffix=".db")
    try:
        s = snap(code="3264", price=198.5, change_rate=-7.24, aflow=810,
                 total_volume=4952, ma20=210.0)
        run_eod_stamp(db, [s], 30, trade_date="2026-07-18")
        run_eod_stamp(db, [s], 30, trade_date="2026-07-20")
        assert len(load_eod(db, "2026-07-18")) == 1
        assert len(load_eod(db, "2026-07-20")) == 1
        # 兩天分開存在
        conn = _sq.connect(db)
        total = conn.execute("SELECT COUNT(*) FROM intraday_eod").fetchone()[0]
        conn.close()
        assert total == 2
    finally:
        if _os.path.exists(db): _os.remove(db)

def test_eod_same_day_rerun_updates():
    """同日重跑 → 更新當日，不重複列。"""
    db = tempfile.mktemp(suffix=".db")
    try:
        s1 = snap(code="3264", price=198.5, change_rate=-7.24, aflow=810,
                  total_volume=4952, ma20=210.0)
        s2 = snap(code="3264", price=200.0, change_rate=-6.0, aflow=900,
                  total_volume=5000, ma20=210.0)
        run_eod_stamp(db, [s1], 30, trade_date="2026-07-20")
        run_eod_stamp(db, [s2], 30, trade_date="2026-07-20")
        rows = load_eod(db, "2026-07-20")
        assert len(rows) == 1              # 同日同檔只一列
        assert rows[0]["aflow"] == 900     # 更新為最新
    finally:
        if _os.path.exists(db): _os.remove(db)


# ================= 跨日追蹤 =================
from app.eod_stamp import load_stock_history, list_trade_dates, stock_trend_summary

def test_stock_history_and_trend():
    import tempfile as _tf, os as _o
    db = _tf.mktemp(suffix=".db")
    try:
        # 同檔三天：排除→觀察→可操作（爬升）
        d1 = snap(code="9999", price=100, change_rate=-9.5, aflow=50000,
                  total_volume=100000, ma20=110.0)   # 排除(極端)
        d2 = snap(code="9999", price=105, change_rate=3.0, aflow=1500,
                  total_volume=8000, ma20=None)       # 觀察(真攻擊,MA20未接)
        d3 = snap(code="9999", price=108, change_rate=-7.0, aflow=2000,
                  total_volume=5000, ma20=None)       # 可操作(強惜售)
        run_eod_stamp(db, [d1], 30, "2026-07-16")
        run_eod_stamp(db, [d2], 50, "2026-07-17")
        run_eod_stamp(db, [d3], 30, "2026-07-18")
        hist = load_stock_history(db, "9999", days=20)
        assert len(hist) == 3
        assert hist[0]["trade_date"] == "2026-07-16"   # 由舊到新
        assert hist[-1]["trade_date"] == "2026-07-18"
        dates = list_trade_dates(db)
        assert dates == ["2026-07-18","2026-07-17","2026-07-16"]  # 新到舊
        summary = stock_trend_summary(db, "9999", days=5)
        assert summary["trend"] == "分類爬升"
        assert summary["latest_group"] == "可操作"
    finally:
        if _o.path.exists(db): _o.remove(db)

def test_trend_persistent_actionable():
    import tempfile as _tf, os as _o
    db = _tf.mktemp(suffix=".db")
    try:
        for d in ["2026-07-16","2026-07-17","2026-07-18"]:
            s = snap(code="8888", price=200, change_rate=-7.0, aflow=2000,
                     total_volume=5000, ma20=None)   # 強惜售可操作
            run_eod_stamp(db, [s], 30, d)
        assert stock_trend_summary(db, "8888", 5)["trend"] == "持續可操作"
    finally:
        if _o.path.exists(db): _o.remove(db)


# ================= 明日觀察清單 =================
from app.twse_fetch import accumulate_streak
from app.tomorrow_watchlist import build_watchlist, READY, WATCH, PULLBACK_WATCH
from app.tomorrow_watchlist import PASS as WL_PASS

def test_accumulate_streak():
    assert accumulate_streak(100, 3) == 4      # 連買延續
    assert accumulate_streak(-50, 3) == -1     # 買轉賣
    assert accumulate_streak(0, 3) == 0        # 中斷歸零
    assert accumulate_streak(100, -2) == 1     # 賣轉買

def _seed_eod(db, date="2026-07-20"):
    from app.eod_stamp import run_eod_stamp
    snaps = [
        snap(code="A_act", price=200, change_rate=-7.0, aflow=2000,
             total_volume=5000, ma20=None),      # 可操作/強惜售
        snap(code="B_atk", price=140, change_rate=3.2, aflow=90,
             total_volume=400, ma20=139.0),      # 可操作/真攻擊
        snap(code="C_wat", price=328, change_rate=2.8, aflow=1722,
             total_volume=31692, ma20=None),     # 觀察(真攻擊MA20未接)
    ]
    run_eod_stamp(db, snaps, thermometer_score=30, trade_date=date)

def test_watchlist_ready_needs_chip():
    """有法人買超 → 強惜售可操作進 Ready。"""
    import tempfile, os
    db = tempfile.mktemp(suffix=".db")
    try:
        _seed_eod(db)
        inst = {"A_act": {"foreign":500,"trust":100,"dealer":0,"total":600}}
        wl = build_watchlist(db, "2026-07-20", institutional=inst)
        a = next(r for r in wl if r["code"]=="A_act")
        assert a["tomorrow_state"] == READY
        assert a["inst_total"] == 600
    finally:
        if os.path.exists(db): os.remove(db)

def test_watchlist_no_chip_degrades_to_watch():
    """TWSE 抓不到 → 最多 Watch，不給 Ready，且不崩潰。"""
    import tempfile, os
    db = tempfile.mktemp(suffix=".db")
    try:
        _seed_eod(db)
        wl = build_watchlist(db, "2026-07-20", institutional=None)  # 沒抓到
        for r in wl:
            assert r["tomorrow_state"] in (WATCH, WL_PASS)   # 絕不 Ready
            assert r["foreign"] is None                   # 標「—」
            assert "缺籌碼" in r["data_completeness"]
    finally:
        if os.path.exists(db): os.remove(db)

def test_watchlist_true_attack_pullback():
    """真攻擊+法人買超 → Pullback Watch（防追高）。"""
    import tempfile, os
    from app.eod_stamp import run_eod_stamp
    db = tempfile.mktemp(suffix=".db")
    try:
        # 攻擊盤下的真攻擊股（一致情境）
        sB = snap(code="B_atk", price=140, change_rate=3.2, aflow=90,
                  total_volume=400, ma20=139.0)
        run_eod_stamp(db, [sB], thermometer_score=68, trade_date="2026-07-20")  # 攻擊盤
        inst = {"B_atk": {"foreign":800,"trust":0,"dealer":0,"total":800}}
        wl = build_watchlist(db, "2026-07-20", institutional=inst)
        b = next(r for r in wl if r["code"]=="B_atk")
        assert b["tomorrow_state"] == PULLBACK_WATCH
    finally:
        if os.path.exists(db): os.remove(db)

def test_watchlist_seller_pass():
    """法人賣超 → Pass。"""
    import tempfile, os
    db = tempfile.mktemp(suffix=".db")
    try:
        _seed_eod(db)
        inst = {"A_act": {"foreign":-900,"trust":0,"dealer":0,"total":-900}}
        wl = build_watchlist(db, "2026-07-20", institutional=inst)
        a = next(r for r in wl if r["code"]=="A_act")
        assert a["tomorrow_state"] == WL_PASS
    finally:
        if os.path.exists(db): os.remove(db)

def test_watchlist_excludes_reject_group():
    """排除群不進明日清單。"""
    import tempfile, os
    from app.eod_stamp import run_eod_stamp
    db = tempfile.mktemp(suffix=".db")
    try:
        # 極端價→排除
        s = snap(code="EXC", price=130, change_rate=-9.7, aflow=178306,
                 total_volume=281032, ma20=140.0)
        run_eod_stamp(db, [s], 30, "2026-07-20")
        wl = build_watchlist(db, "2026-07-20", institutional={})
        assert all(r["code"] != "EXC" for r in wl)   # 排除群不出現
    finally:
        if os.path.exists(db): os.remove(db)

if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); passed += 1; print("  PASS  " + fn.__name__)
        except AssertionError:
            print("  FAIL  " + fn.__name__); traceback.print_exc()
    print("\n%d/%d passed" % (passed, len(fns)))
