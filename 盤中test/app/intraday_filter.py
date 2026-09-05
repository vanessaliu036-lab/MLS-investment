# -*- coding: utf-8 -*-
"""
intraday_filter.py — 盤中篩選邏輯公式核心（v4：三態邏輯 + MA20 + 極端價防護）

三次重大修正累積：
  v2 介面防呆：passes_filters 回現成 passed/failed 清單，杜絕「印 key 名稱」反相 bug。
  v3 盤勢模式：象限篩選依溫度計自動切換（攻擊盤真攻擊 / 防守盤強惜售）。
  v4 三態 + 防護（本次）：
     1. 三態邏輯 PASS / FAIL / NO_DATA —— 把「算不出」和「不通過」分開。
        MA20/成交量未接入、極端價失真 → NO_DATA(－)，不是 FAIL(✗)。
     2. 極端價防護：逼近跌停/漲停時 aflow 方向不可信，相關條件判 NO_DATA。
        對齊系統鐵律：NO_DATA 絕不當 PASS。
     3. all_pass 定義收緊：需「無 NO_DATA 且全 PASS」，算不出的不算通過。

設計原則不變：
1. 每個條件 = 純函式，無副作用、無 DB、無網路 → 可單獨 assert 驗算。
2. 只吃盤中訂閱推播算得到的欄位 + 盤前算好快取的常數（MA20）。
3. aflow 兩種算法都提供，互相對照驗算（偵錯用）。
"""

from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

# 三態常數
PASS = "PASS"
FAIL = "FAIL"
NO_DATA = "NO_DATA"

# 顯示符號
_MARK = {PASS: "\u2713", FAIL: "\u2717", NO_DATA: "\u2014"}  # ✓ / ✗ / —

# 門檻（集中調）
AFLOW_INTENSITY_MIN = 10.0    # aflow 強度佔比門檻（%）
EXTREME_PCT = 9.0             # 逼近漲跌停判定：|漲跌%| >= 此值 → aflow 失真

# 法人校驗門檻（2026-07-20 華邦電教訓：+14162 寫成 -161，方向 + 數量級）
# 注意：大型權值股法人日買賣超動輒 3-5 萬張（佔成交量 30-50%）是常態，
# 1% / 10% 這種比例門檻會誤擋正常大型股。改用「絕對數量級 + 方向矛盾」邏輯。
INST_ABS_HARD_RATIO = 1.0      # 單一法人 |買賣超| > 成交量×100% → 數量級不可能，硬擋
INST_TOTAL_SELL_BLOCK = 0.03   # 法人合計賣超 / 成交量 > 3% → 屏蔽「惜售」標籤
INST_DIR_CONFLICT_ABS = 10000  # 合計賣超時，單一法人買超 > 1 萬張 → 方向矛盾警告（小型股預設）
DAILY_CHANGE_ALERT = 3.0       # 法人日變動 > 前日 300% → 強制人工確認


# =====================================================================
# 一、aflow —— 兩種算法對照
# =====================================================================

def aflow_from_sides(bid_total_vol: int, ask_total_vol: int) -> int:
    """算法 A：官方買賣盤累積量相減（最穩，主用）。正=買方主動多，負=賣方主動多。
    鐵律：買賣盤積極度估算，非法人淨買賣，盤中流出 != 派發。"""
    return int(bid_total_vol) - int(ask_total_vol)


def aflow_official(bid_total_vol: int, ask_total_vol: int) -> int:
    return aflow_from_sides(bid_total_vol, ask_total_vol)


def aflow_ticktype(tick_type_stream: List[Tuple[int, int]]) -> int:
    """算法 B：逐筆 TickType 累加（偵錯對照）。1外盤+ / 2內盤- / 0不計。"""
    acc = 0
    for tick_type, volume in tick_type_stream:
        if tick_type == 1:
            acc += int(volume)
        elif tick_type == 2:
            acc -= int(volume)
    return acc


def aflow_reconcile(aflow_a: int, aflow_b: int, tol: int = 5) -> dict:
    """兩算法對照，背離 → 疑訂閱異常（斷線/漏筆）。"""
    diff = abs(aflow_a - aflow_b)
    return {"aflow_official": aflow_a, "aflow_ticktype": aflow_b,
            "diff": diff, "diverged": diff > tol}


# =====================================================================
# 二、極端價防護 —— 跌停/漲停 aflow 失真判定
# =====================================================================

def is_extreme_price(change_rate: float) -> bool:
    """
    逼近漲跌停判定。跌停時買方掛單被動成交會被記成外盤(主動買)，
    漲停時反之，aflow 方向嚴重失真，不可用來判吸籌/派發。
    |漲跌%| >= EXTREME_PCT → 極端價，相關訊號降級 NO_DATA。
    """
    return abs(change_rate) >= EXTREME_PCT


# =====================================================================
# 三、盤中即時衍生欄位
# =====================================================================

def dist_ma20(price: float, ma20: Optional[float]) -> Optional[float]:
    """離月線 %（正=站上）。ma20 未接入(None)→None，不補造。"""
    if ma20 is None:
        return None
    return round((price - ma20) / ma20 * 100, 2)


def volume_ratio(cur_total_volume: int, yesterday_volume: int) -> Optional[float]:
    """量比。昨量未接入→None。"""
    if not yesterday_volume:
        return None
    return round(cur_total_volume / yesterday_volume, 2)


def aflow_intensity(aflow: int, total_volume: int) -> Optional[float]:
    """aflow 強度佔比 % = aflow/量。解決大跌盤絕對值分不出強弱。量未接入→None。"""
    if not total_volume:
        return None
    return round(aflow / total_volume * 100, 1)


def proxy_quadrant(aflow: int, change_rate: float) -> str:
    """代理象限（收盤定案，UI 標未定案）。
    aflow>0漲→真攻擊 / aflow<0漲→假紅 / aflow>0跌→惜售 / aflow<0跌→休息。"""
    up = change_rate > 0
    inflow = aflow > 0
    if inflow and up:
        return "真攻擊"
    if not inflow and up:
        return "假紅"
    if inflow and not up:
        return "惜售"
    return "休息"


# =====================================================================
# 四、盤勢模式
# =====================================================================

REGIME_ATTACK = "攻擊盤"
REGIME_DEFENSE = "防守盤"
REGIME_RANGE = "震盪盤"


def market_regime(thermometer_score: int) -> str:
    """溫度計 >60 攻擊 / <40 防守 / 其餘震盪。"""
    if thermometer_score > 60:
        return REGIME_ATTACK
    if thermometer_score < 40:
        return REGIME_DEFENSE
    return REGIME_RANGE


# =====================================================================
# 五、StockSnap
# =====================================================================

@dataclass
class StockSnap:
    code: str
    track: str                       # "engine" / "attack"
    price: float
    change_rate: float
    aflow: int
    total_volume: int = 0
    ma20: Optional[float] = None
    trigger_price: Optional[float] = None
    atr_stop: Optional[float] = None
    inst_buy_days: int = 0
    # 法人即時籌碼（None = 沒抓到，0 = 抓到且確認為零）
    inst_foreign: Optional[int] = None      # 外資買賣超（張）
    inst_trust: Optional[int] = None        # 投信買賣超（張）
    inst_dealer: Optional[int] = None       # 自營商買賣超（張）
    inst_net_total: Optional[int] = None    # 三大法人合計（自動 sum 或餵入）


# =====================================================================
# 五點五、 法人數據校驗（2026-07-20 華邦電教訓：先確保數據正確）
# =====================================================================

def compute_inst_net_total(s: StockSnap) -> Optional[int]:
    """三大法人合計 = 外資+投信+自營。任一缺 → 合計也缺（不補造）。"""
    if s.inst_foreign is None or s.inst_trust is None or s.inst_dealer is None:
        return None
    return s.inst_foreign + s.inst_trust + s.inst_dealer


def validate_inst_data(s: StockSnap) -> dict:
    """
    三層校驗（華邦電 +14162 / -161 教訓）：
      規則1：單一法人 |買賣超| > 成交量×100% → 數量級不可能（機構不可能一天買超過整個市場一天量）→ 硬擋
      規則2：合計賣超時，單一法人買超 > 10000 張 → 方向矛盾警告（小型股預設門檻，大型股可調）
      規則3：合計單日絕對值 > 前日 300% → 警告（需 inst_prev_total 配合）
    回傳：
      {
        "ok": bool,                  # 完全沒事
        "warnings": [str],            # UI 顯示用（黃字）
        "hard_block": bool,           # 數據明確錯 → 惜售/攻擊全降級
        "reasons": [str],             # 校驗細節（debug 用）
      }
    重要：大型權值股法人日買賣超 3-5 萬張佔成交量 30-50% 是常態，不觸發 1% / 10% 等比例門檻。
    """
    warnings: list = []
    hard_block = False
    reasons: list = []
    vol = s.total_volume or 0
    net = compute_inst_net_total(s)

    # 沒法人資料 → 校驗略過，惜售交給「資料缺失就降級」處理
    if net is None or s.inst_foreign is None or s.inst_trust is None or s.inst_dealer is None:
        return {"ok": True, "warnings": [], "hard_block": False,
                "reasons": ["無法人資料，校驗略過"]}

    # 規則 1：單一法人絕對值 vs 成交量（只擋「不可能」的數量級）
    for label, val in (("外資", s.inst_foreign), ("投信", s.inst_trust), ("自營", s.inst_dealer)):
        if vol and abs(val) > vol * INST_ABS_HARD_RATIO:
            hard_block = True
            warnings.append(f"🛑 {label} 買賣超 {val:+} 張，數量級不可能（{val:,} > 當日成交量 {vol:,}），數據源錯了")
            reasons.append(f"{label}={val} > vol×100%")

    # 規則 2：合計賣超時，單一法人買超大 → 方向矛盾
    if net < 0:
        for label, val in (("外資", s.inst_foreign), ("投信", s.inst_trust), ("自營", s.inst_dealer)):
            if val > INST_DIR_CONFLICT_ABS:
                warnings.append(f"⚠️ 法人合計賣超 {net:+} 張，{label} 卻買超 {val:+} 張，方向矛盾請複核")
                reasons.append(f"合計<0 但 {label}={val}>{INST_DIR_CONFLICT_ABS}")

    # 規則 3：日變動範圍（需 inst_prev_total 配合，沒給就跳過）
    prev = getattr(s, "inst_prev_total", None)
    if prev not in (None, 0) and prev != 0:
        change_ratio = abs(net - prev) / abs(prev)
        if change_ratio > DAILY_CHANGE_ALERT:
            warnings.append(f"⚠️ 法人合計 {prev:+}→{net:+}，單日變動 {change_ratio:.0%}，超過 300% 請人工確認")
            reasons.append(f"日變動 {change_ratio:.0%}")

    return {"ok": not warnings, "warnings": warnings, "hard_block": hard_block, "reasons": reasons}


def inst_sell_blocks_absorb(s: StockSnap) -> bool:
    """
    法人合計賣超屏蔽惜售（第二層強制排除條件）：
      法人合計賣超絕對值 > 成交量 × 3% → 不論 aflow 多正都屏蔽惜售
    缺資料 → False（不擋，交給資料缺失降級）
    """
    net = compute_inst_net_total(s)
    if net is None or net >= 0:
        return False
    vol = s.total_volume or 0
    if not vol:
        return False
    return abs(net) > vol * INST_TOTAL_SELL_BLOCK


# =====================================================================
# 六、三態篩選條件 —— 每條回傳 PASS / FAIL / NO_DATA
# =====================================================================

def st_aflow_positive(s: StockSnap) -> str:
    """主動差>0。極端價→NO_DATA（aflow 失真）。"""
    if is_extreme_price(s.change_rate):
        return NO_DATA
    return PASS if s.aflow > 0 else FAIL


def st_above_ma20(s: StockSnap) -> str:
    """站上月線。MA20 未接入→NO_DATA（不是 FAIL）。"""
    d = dist_ma20(s.price, s.ma20)
    if d is None:
        return NO_DATA
    return PASS if d > 0 else FAIL


def st_aflow_intensity(s: StockSnap) -> str:
    """吸籌強度足。量未接入→NO_DATA；極端價→NO_DATA（aflow 失真連帶佔比失真）。"""
    if is_extreme_price(s.change_rate):
        return NO_DATA
    intensity = aflow_intensity(s.aflow, s.total_volume)
    if intensity is None:
        return NO_DATA
    return PASS if intensity >= AFLOW_INTENSITY_MIN else FAIL


def st_regime_quadrant(s: StockSnap, regime: str) -> str:
    """
    依盤勢的象限條件（三態）。極端價→NO_DATA（象限由 aflow 推得，失真）。
        攻擊盤→真攻擊 / 防守盤→強惜售(惜售+強度足) / 震盪盤→兩者皆可
    2026-07-20 華邦電教訓新增三層守門：
      a) 法人數據 hard_block（絕對值超 10%）→ NO_DATA（不論象限）
      b) 法人合計賣超屏蔽惜售（賣超 > 量×3%）→ 惜售軸降級 FAIL
      c) 法人資料缺失 → 守門略過（惜售仍由 aflow 強度決）
    """
    if is_extreme_price(s.change_rate):
        return NO_DATA
    v = validate_inst_data(s)
    if v["hard_block"]:
        return NO_DATA
    q = proxy_quadrant(s.aflow, s.change_rate)
    intensity = aflow_intensity(s.aflow, s.total_volume)
    raw_strong = (q == "惜售" and intensity is not None
                  and intensity >= AFLOW_INTENSITY_MIN)
    # 法人合計賣超屏蔽惜售（守門 b）
    strong_absorb = raw_strong and not inst_sell_blocks_absorb(s)
    true_attack = (q == "真攻擊")
    if regime == REGIME_ATTACK:
        ok = true_attack
    elif regime == REGIME_DEFENSE:
        ok = strong_absorb
    else:
        ok = true_attack or strong_absorb
    return PASS if ok else FAIL


def _regime_quadrant_label(regime: str) -> str:
    return {REGIME_ATTACK: "象限真攻擊", REGIME_DEFENSE: "強惜售承接",
            REGIME_RANGE: "攻擊或強惜售"}[regime]


# 相容用布林包裝（舊呼叫端 / 狀態機用）
def cond_aflow_positive(s): return st_aflow_positive(s) == PASS
def cond_above_ma20(s): return st_above_ma20(s) == PASS
def cond_aflow_intensity(s): return st_aflow_intensity(s) == PASS
def cond_quadrant_attack(s):
    """真攻擊軸：極端價/hard_block→False（不誤判），不靠 validate 的 hard_block 全 NO_DATA"""
    if is_extreme_price(s.change_rate): return False
    if validate_inst_data(s)["hard_block"]: return False
    return proxy_quadrant(s.aflow, s.change_rate) == "真攻擊"
def cond_strong_absorb(s):
    """強惜售：極端價/hard_block/法人賣超屏蔽 全 False"""
    if is_extreme_price(s.change_rate): return False
    if validate_inst_data(s)["hard_block"]: return False
    if inst_sell_blocks_absorb(s): return False
    return proxy_quadrant(s.aflow, s.change_rate) == "惜售" and cond_aflow_intensity(s)


# =====================================================================
# 七、passes_filters —— 三態 + 防呆清單 + 盤勢
# =====================================================================

def passes_filters(s: StockSnap, regime: Optional[str] = None) -> dict:
    """
    三態篩選。回傳前端無腦印、接不反的結構：
        states  : {key: PASS/FAIL/NO_DATA}
        passed  : [PASS 條件中文標籤]（綠）
        failed  : [FAIL 條件中文標籤]（灰）
        no_data : [NO_DATA 條件中文標籤]（－，含未接入/極端價失真）
        all_pass: bool  ← 需「無 NO_DATA 且全 PASS」，算不出的不算通過
        display : "✓主動差>0　✗站上MA20　—吸籌強度足"（一行直接貼）
        regime  : 盤勢
        extreme : bool  ← 是否極端價（跌停/漲停），UI 標「極端價·訊號不可信」
    regime=None → 象限用固定真攻擊（向後相容）。
    """
    rules: List[Tuple[str, str, callable]] = [
        ("aflow_positive",  "主動差>0",   st_aflow_positive),
        ("above_ma20",      "站上MA20",   st_above_ma20),
        ("aflow_intensity", "吸籌強度足", st_aflow_intensity),
    ]
    if regime is None:
        rules.append(("quadrant_attack", "象限真攻擊",
                      lambda s: PASS if cond_quadrant_attack(s) else FAIL))
    else:
        rules.append(("regime_quadrant", _regime_quadrant_label(regime),
                      lambda s, rg=regime: st_regime_quadrant(s, rg)))

    states: Dict[str, str] = {}
    passed: List[str] = []
    failed: List[str] = []
    no_data: List[str] = []
    parts: List[str] = []

    for key, label, fn in rules:
        st = fn(s)
        states[key] = st
        parts.append(_MARK[st] + label)
        if st == PASS:
            passed.append(label)
        elif st == FAIL:
            failed.append(label)
        else:
            no_data.append(label)

    all_pass = all(v == PASS for v in states.values())

    inst_v = validate_inst_data(s)
    inst_blocked = inst_sell_blocks_absorb(s)

    return {
        "states": states,
        "passed": passed,
        "failed": failed,
        "no_data": no_data,
        "all_pass": all_pass,
        "display": "\u3000".join(parts),
        "regime": regime,
        "extreme": is_extreme_price(s.change_rate),
        # 法人校驗結果（2026-07-20 新增）
        "inst_net_total": compute_inst_net_total(s),
        "inst_validation": inst_v,
        "inst_sell_blocks_absorb": inst_blocked,
    }


def rank_potential(snaps: List[StockSnap]) -> List[dict]:
    """潛力排序：主排吸籌強度佔比，次排抗跌。極端價股標記但不參與（訊號不可信）。"""
    rows = []
    for s in snaps:
        intensity = aflow_intensity(s.aflow, s.total_volume)
        extreme = is_extreme_price(s.change_rate)
        rows.append({
            "code": s.code,
            "intensity": intensity,
            "change_rate": s.change_rate,
            "aflow": s.aflow,
            "quadrant": proxy_quadrant(s.aflow, s.change_rate),
            "extreme": extreme,
            "intensity_pass": (not extreme and intensity is not None
                               and intensity >= AFLOW_INTENSITY_MIN),
        })
    # 極端價排最後（訊號不可信）；再主排強度、次排抗跌
    rows.sort(key=lambda r: (
        0 if r["extreme"] else 1,
        r["intensity"] if r["intensity"] is not None else -1e9,
        r["change_rate"],
    ), reverse=True)
    return rows


# =====================================================================
# 八、雙軌狀態機（華邦電教訓：軌道不可混）
# =====================================================================

STATES = ["觀察中", "訊號成立", "已進場", "停損觸發", "停利觸發", "出場"]


def engine_signal(s: StockSnap, min_inst_buy_days: int = 1) -> bool:
    """引擎軌訊號：站上月線 + 昨日法人連買。MA20 未接入→cond_above_ma20 False→不成立。"""
    return cond_above_ma20(s) and s.inst_buy_days >= min_inst_buy_days


def engine_stop(s: StockSnap) -> bool:
    """引擎軌停損：跌破月線。MA20 未接入→無法判→False（不誤停）。"""
    d = dist_ma20(s.price, s.ma20)
    return d is not None and d < 0


def attack_signal(s: StockSnap) -> bool:
    return s.trigger_price is not None and s.price >= s.trigger_price


def attack_stop(s: StockSnap) -> bool:
    return s.atr_stop is not None and s.price < s.atr_stop


def next_state(prev_state: str, s: StockSnap) -> str:
    is_engine = s.track == "engine"
    stopped = engine_stop(s) if is_engine else attack_stop(s)
    signaled = engine_signal(s) if is_engine else attack_signal(s)
    if prev_state in ("已進場", "訊號成立") and stopped:
        return "停損觸發"
    if prev_state == "觀察中" and signaled:
        return "訊號成立"
    return prev_state
