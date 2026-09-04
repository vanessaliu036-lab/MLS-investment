# -*- coding: utf-8 -*-
"""
intraday_filter.py — 盤中篩選邏輯公式核心

設計原則：
1. 每個條件 = 一個純函式，無 DB、無網路，可單獨驗算。
2. 只吃盤中訂閱推播算得到的欄位 + 盤前算好快取的常數。
3. A-flow 兩種算法互相對照驗算。

Shioaji TickSTKv1 官方語意：
    bid_side_total_vol = 買盤成交總量（整股：張）
    ask_side_total_vol = 賣盤成交總量（整股：張）
    tick_type = 1 外盤（買方主動）、2 內盤（賣方主動）、0 無法判定

因此 canonical A-flow：
    A-flow = bid_side_total_vol − ask_side_total_vol
           = 主動買成交張數 − 主動賣成交張數
正值代表盤中買方成交積極度較高；負值代表賣方成交積極度較高。
它不是法人買賣超。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

PASS = "PASS"
FAIL = "FAIL"
NO_DATA = "NO_DATA"
_MARK = {PASS: "✓", FAIL: "✗", NO_DATA: "—"}
EXTREME_PCT = 9.0


# =====================================================================
# 一、主動買賣差 A-flow —— 兩種算法，互相對照
# =====================================================================

def aflow_official(bid_side_total_vol: int, ask_side_total_vol: int) -> int:
    """官方累積成交側量算法。

    Shioaji 定義：bid_side_total_vol=買盤成交總量，ask_side_total_vol=賣盤成交總量。
    A-flow = 買盤成交總量 − 賣盤成交總量。
    正 = 主動買成交較多；負 = 主動賣成交較多。
    """
    return int(bid_side_total_vol) - int(ask_side_total_vol)


def aflow_ticktype(tick_type_stream) -> int:
    """逐筆 TickType 累加，用來對照官方累積成交側量。

    tick_type == 1（外盤／買方主動）→ +volume
    tick_type == 2（內盤／賣方主動）→ -volume
    tick_type == 0（無法判定）→ 0
    """
    acc = 0
    for tick_type, volume in tick_type_stream:
        if tick_type == 1:
            acc += int(volume)
        elif tick_type == 2:
            acc -= int(volume)
    return acc


def aflow_reconcile(aflow_a: int, aflow_b: int, tol: int = 5) -> dict:
    """兩種算法背離時回報，避免方向或訂閱異常被靜默吞掉。"""
    diff = abs(aflow_a - aflow_b)
    return {
        "aflow_official": aflow_a,
        "aflow_ticktype": aflow_b,
        "diff": diff,
        "diverged": diff > tol,
    }


# =====================================================================
# 二、盤中即時衍生欄位公式
# =====================================================================

def dist_ma20(price: float, ma20: Optional[float]) -> Optional[float]:
    if not ma20:
        return None
    return round((price - ma20) / ma20 * 100, 2)


def volume_ratio(cur_total_volume: int, yesterday_volume: int) -> Optional[float]:
    if not yesterday_volume:
        return None
    return round(cur_total_volume / yesterday_volume, 2)


def aflow_intensity(aflow: int, total_volume: int) -> Optional[float]:
    if not total_volume:
        return None
    return round(aflow / total_volume * 100, 1)


def is_extreme_price(change_rate: float) -> bool:
    return abs(change_rate) >= EXTREME_PCT


def proxy_quadrant(aflow: int, change_rate: float) -> str:
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
# 三、篩選條件
# =====================================================================

def cond_aflow_positive(s, _unused=None) -> bool:
    if hasattr(s, "aflow"):
        return st_aflow_positive(s) == PASS
    return s > 0


def cond_above_ma20(s, ma20: Optional[float] = None) -> bool:
    if hasattr(s, "price"):
        return st_above_ma20(s) == PASS
    price = s
    line = s.ma20 if hasattr(s, "ma20") else ma20
    d = dist_ma20(price, line)
    return d is not None and d > 0


def cond_quadrant_attack(s, change_rate: Optional[float] = None) -> bool:
    if hasattr(s, "aflow"):
        return (not is_extreme_price(s.change_rate)
                and proxy_quadrant(s.aflow, s.change_rate) == "真攻擊")
    aflow = s
    change = s.change_rate if hasattr(s, "change_rate") else change_rate
    return proxy_quadrant(aflow, change) == "真攻擊"


AFLOW_INTENSITY_MIN = 10.0


def cond_aflow_intensity(s: "StockSnap") -> bool:
    return st_aflow_intensity(s) == PASS


def cond_strong_absorb(s: "StockSnap") -> bool:
    return (not is_extreme_price(s.change_rate)
            and proxy_quadrant(s.aflow, s.change_rate) == "惜售"
            and cond_aflow_intensity(s))


REGIME_ATTACK = "攻擊盤"
REGIME_DEFENSE = "防守盤"
REGIME_RANGE = "震盪盤"


def market_regime(thermometer_score: int) -> str:
    if thermometer_score > 60:
        return REGIME_ATTACK
    if thermometer_score < 40:
        return REGIME_DEFENSE
    return REGIME_RANGE


def cond_regime_quadrant(s: "StockSnap", regime: str) -> bool:
    if regime == REGIME_ATTACK:
        return cond_quadrant_attack(s)
    if regime == REGIME_DEFENSE:
        return cond_strong_absorb(s)
    return cond_quadrant_attack(s) or cond_strong_absorb(s)


def _regime_quadrant_label(regime: str) -> str:
    return {
        REGIME_ATTACK: "象限真攻擊",
        REGIME_DEFENSE: "強惜售承接",
        REGIME_RANGE: "攻擊或強惜售",
    }[regime]


def st_aflow_positive(s: "StockSnap") -> str:
    if is_extreme_price(s.change_rate):
        return NO_DATA
    return PASS if s.aflow > 0 else FAIL


def st_above_ma20(s: "StockSnap") -> str:
    d = dist_ma20(s.price, s.ma20)
    if d is None:
        return NO_DATA
    return PASS if d > 0 else FAIL


def st_aflow_intensity(s: "StockSnap") -> str:
    if is_extreme_price(s.change_rate):
        return NO_DATA
    intensity = aflow_intensity(s.aflow, s.total_volume)
    if intensity is None:
        return NO_DATA
    return PASS if intensity >= AFLOW_INTENSITY_MIN else FAIL


def st_regime_quadrant(s: "StockSnap", regime: str) -> str:
    if is_extreme_price(s.change_rate):
        return NO_DATA
    q = proxy_quadrant(s.aflow, s.change_rate)
    intensity = aflow_intensity(s.aflow, s.total_volume)
    strong_absorb = (q == "惜售" and intensity is not None
                     and intensity >= AFLOW_INTENSITY_MIN)
    true_attack = q == "真攻擊"
    ok = (true_attack if regime == REGIME_ATTACK else
          strong_absorb if regime == REGIME_DEFENSE else
          true_attack or strong_absorb)
    return PASS if ok else FAIL


FILTER_RULES: List[Tuple[str, str, object]] = [
    ("aflow_positive", "A-flow>0", cond_aflow_positive),
    ("above_ma20", "站上MA20", cond_above_ma20),
    ("quadrant_attack", "象限真攻擊", cond_quadrant_attack),
    ("aflow_intensity", "A-flow強度足", cond_aflow_intensity),
]

BASE_FILTER_RULES: List[Tuple[str, str, object]] = [
    ("aflow_positive", "A-flow>0", cond_aflow_positive),
    ("above_ma20", "站上MA20", cond_above_ma20),
    ("aflow_intensity", "A-flow強度足", cond_aflow_intensity),
]


@dataclass
class StockSnap:
    code: str
    track: str
    price: float
    change_rate: float
    aflow: int
    total_volume: int = 0
    ma20: Optional[float] = None
    trigger_price: Optional[float] = None
    atr_stop: Optional[float] = None
    inst_buy_days: int = 0


def passes_filters(s: StockSnap, regime: Optional[str] = None) -> dict:
    rules: List[Tuple[str, str, object]] = [
        ("aflow_positive", "A-flow>0", st_aflow_positive),
        ("above_ma20", "站上MA20", st_above_ma20),
        ("aflow_intensity", "A-flow強度足", st_aflow_intensity),
    ]
    if regime is None:
        rules.append(("quadrant_attack", "象限真攻擊",
                      lambda snap: PASS if cond_quadrant_attack(snap) else FAIL))
    else:
        rules.append(("regime_quadrant", _regime_quadrant_label(regime),
                      lambda snap, rg=regime: st_regime_quadrant(snap, rg)))

    states: Dict[str, str] = {}
    checks: Dict[str, bool] = {}
    passed: List[str] = []
    failed: List[str] = []
    display: List[str] = []
    for key, label, fn in rules:
        state = fn(s)
        states[key] = state
        checks[key] = state == PASS
        display.append(_MARK[state] + label)
        if state == PASS:
            passed.append(label)
        elif state == FAIL:
            failed.append(label)
    no_data = [label for key, label, fn in rules if states[key] == NO_DATA]
    return {
        "states": states,
        "checks": checks,
        "passed": passed,
        "failed": failed,
        "all_pass": all(checks.values()),
        "display": "　".join(display),
        "regime": regime,
        "no_data": no_data,
        "extreme": is_extreme_price(s.change_rate),
        **checks,
    }


def rank_potential(snaps: List["StockSnap"]) -> List[dict]:
    rows = []
    for s in snaps:
        intensity = aflow_intensity(s.aflow, s.total_volume)
        rows.append({
            "code": s.code,
            "intensity": intensity,
            "change_rate": s.change_rate,
            "aflow": s.aflow,
            "quadrant": proxy_quadrant(s.aflow, s.change_rate),
            "intensity_pass": intensity is not None and intensity >= AFLOW_INTENSITY_MIN,
            "extreme": is_extreme_price(s.change_rate),
        })
    rows.sort(key=lambda r: (
        0 if r["extreme"] else 1,
        r["intensity"] if r["intensity"] is not None else -1e9,
        r["change_rate"],
    ), reverse=True)
    return rows


# =====================================================================
# 四、雙軌狀態機
# =====================================================================
STATES = ["觀察中", "訊號成立", "已進場", "停損觸發", "停利觸發", "出場"]


def engine_signal(s: StockSnap, min_inst_buy_days: int = 1) -> bool:
    return cond_above_ma20(s.price, s.ma20) and s.inst_buy_days >= min_inst_buy_days


def engine_stop(s: StockSnap) -> bool:
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
