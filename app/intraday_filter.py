# -*- coding: utf-8 -*-
"""
intraday_filter.py — 盤中篩選邏輯公式核心

設計原則（對齊 MLS 系統慣例）：
1. 每個條件 = 一個純函式（pure function），無副作用、無 DB、無網路 → 可單獨 assert 驗算。
2. 只吃「盤中訂閱推播算得到的欄位」+「盤前算好快取的常數」，不碰盤後籌碼。
3. 主動買賣差 aflow 兩種算法都提供，互相對照驗算（偵錯用）。

盤中可用欄位（來自 Shioaji 訂閱 tick / bidask）：
    price          即時成交價          (tick.close)
    change_rate    漲跌%               (tick.pct_chg)
    bid_total_vol  內盤/成交在 bid 的累積量 (tick.bid_side_total_vol) → 主動賣
    ask_total_vol  外盤/成交在 ask 的累積量 (tick.ask_side_total_vol) → 主動買
    tick_type      內外盤別 1外/2內/0無  (tick.tick_type)          → 逐筆自累加用

盤前算好快取（非盤中即時，開盤前一次性算好）：
    ma20           月線                盤前用 kbar 算
    trigger_price  攻擊軌突破觸發價      盤前定
    atr_stop       攻擊軌 ATR 停損價     盤前定

盤後蓋章值（盤中一律取昨日，不參與盤中篩選數值計算，只做狀態機門檻參考）：
    inst_buy_days  法人連買天數         db.load_dec_health(昨日)
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

PASS = "PASS"
FAIL = "FAIL"
NO_DATA = "NO_DATA"
_MARK = {PASS: "✓", FAIL: "✗", NO_DATA: "—"}
EXTREME_PCT = 9.0


# =====================================================================
# 一、主動買賣差 aflow —— 兩種算法，互相對照
# =====================================================================

def aflow_official(bid_total_vol: int, ask_total_vol: int) -> int:
    """
    算法 A：官方現成累積量相減（最穩，推薦主用）。
    aflow = ask_side_total_vol − bid_side_total_vol
    正 = 外盤/主動買較多（估流入）；負 = 內盤/主動賣較多（估流出）。

    鐵律：這是「買賣盤積極度估算」，不是法人淨買賣，盤中流出 ≠ 派發。
    """
    return int(ask_total_vol) - int(bid_total_vol)


def aflow_ticktype(tick_type_stream) -> int:
    """
    算法 B：自己逐筆 TickType 累加（可控，偵錯對照用）。
    對一段逐筆序列，每筆帶 (tick_type, volume)：
        tick_type == 1 (外盤/買方主動，成交在 ask) → +volume
        tick_type == 2 (內盤/賣方主動，成交在 bid) → −volume
        tick_type == 0 (無法判定)       → 0（不計）

    tick_type_stream: List[Tuple[int, int]]  # [(tick_type, volume), ...]
    """
    acc = 0
    for tick_type, volume in tick_type_stream:
        if tick_type == 1:
            acc += int(volume)
        elif tick_type == 2:
            acc -= int(volume)
        # tick_type == 0：無法判定，不計
    return acc


def aflow_reconcile(aflow_a: int, aflow_b: int, tol: int = 5) -> dict:
    """
    對照驗算：兩種算法背離時回報，用於偵測「資金流抓不到」的根因。

    背離常見原因：
      - 訂閱斷線 → 逐筆停止進來 → 算法 B 凍結，A 若走查詢也可能凍 → 兩者都停但值不同
      - 累加漏筆 / tick_type=0 佔比高 → B 偏小
    回傳 diverged=True 時，UI 應標「aflow 校驗背離，疑訂閱異常」。
    """
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
    """離月線 %（正 = 站上月線之上）。ma20 盤前算好快取。"""
    if not ma20:
        return None
    return round((price - ma20) / ma20 * 100, 2)


def volume_ratio(cur_total_volume: int, yesterday_volume: int) -> Optional[float]:
    """量比 = 今累積量 / 昨同時累積量。昨量盤前快取。"""
    if not yesterday_volume:
        return None
    return round(cur_total_volume / yesterday_volume, 2)


def aflow_intensity(aflow: int, total_volume: int) -> Optional[float]:
    """aflow 佔成交量百分比；成交量缺失時回 None，不補造。"""
    if not total_volume:
        return None
    return round(aflow / total_volume * 100, 1)


def is_extreme_price(change_rate: float) -> bool:
    """接近漲跌停時，委託結構會令 aflow 方向失真。"""
    return abs(change_rate) >= EXTREME_PCT


def proxy_quadrant(aflow: int, change_rate: float) -> str:
    """
    代理象限（盤中估值，收盤才定案 → UI 必標「代理·未定案」）。
    四象限：資金流向 × 漲跌方向
        aflow>0 & 漲 → 真攻擊
        aflow<0 & 漲 → 假紅（漲但資金流出，留意）
        aflow>0 & 跌 → 惜售（跌但資金流入，低接）
        aflow<0 & 跌 → 休息
    注意：這是代理，開盤壓吸階段可能先顯示假象限，收盤定案為準。
    """
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
# 三、篩選條件 —— 每條一個純布林函式，可單獨驗算
# =====================================================================

def cond_aflow_positive(s, _unused=None) -> bool:
    """條件：主動差 > 0（買方積極）。"""
    if hasattr(s, "aflow"):
        return st_aflow_positive(s) == PASS
    return s > 0


def cond_above_ma20(s, ma20: Optional[float] = None) -> bool:
    """條件：站上月線。"""
    if hasattr(s, "price"):
        return st_above_ma20(s) == PASS
    price = s
    line = s.ma20 if hasattr(s, "ma20") else ma20
    d = dist_ma20(price, line)
    return d is not None and d > 0


def cond_quadrant_attack(s, change_rate: Optional[float] = None) -> bool:
    """條件：代理象限 = 真攻擊。"""
    if hasattr(s, "aflow"):
        return (not is_extreme_price(s.change_rate)
                and proxy_quadrant(s.aflow, s.change_rate) == "真攻擊")
    aflow = s
    change = s.change_rate if hasattr(s, "change_rate") else change_rate
    return proxy_quadrant(aflow, change) == "真攻擊"


# 篩選條件註冊表：UI「符合條件」模式的篩選列直接對應這張表
AFLOW_INTENSITY_MIN = 10.0


def cond_aflow_intensity(s: "StockSnap") -> bool:
    return st_aflow_intensity(s) == PASS


def cond_strong_absorb(s: "StockSnap") -> bool:
    """跌勢中的惜售，且吸籌強度達標，才算強承接。"""
    return (not is_extreme_price(s.change_rate)
            and proxy_quadrant(s.aflow, s.change_rate) == "惜售"
            and cond_aflow_intensity(s))


REGIME_ATTACK = "攻擊盤"
REGIME_DEFENSE = "防守盤"
REGIME_RANGE = "震盪盤"


def market_regime(thermometer_score: int) -> str:
    """溫度計 >60 為攻擊盤、<40 為防守盤，其餘為震盪盤。"""
    if thermometer_score > 60:
        return REGIME_ATTACK
    if thermometer_score < 40:
        return REGIME_DEFENSE
    return REGIME_RANGE


def cond_regime_quadrant(s: "StockSnap", regime: str) -> bool:
    """依盤勢套用真攻擊、強惜售，或兩者皆可。"""
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
    ("aflow_positive", "主動差>0", cond_aflow_positive),
    ("above_ma20", "站上MA20", cond_above_ma20),
    ("quadrant_attack", "象限真攻擊", cond_quadrant_attack),
    ("aflow_intensity", "吸籌強度足", cond_aflow_intensity),
]

BASE_FILTER_RULES: List[Tuple[str, str, object]] = [
    ("aflow_positive", "主動差>0", cond_aflow_positive),
    ("above_ma20", "站上MA20", cond_above_ma20),
    ("aflow_intensity", "吸籌強度足", cond_aflow_intensity),
]


@dataclass
class StockSnap:
    """一檔個股的盤中快照（只含盤中算得到 + 盤前快取的欄位）。"""
    code: str
    track: str            # "engine" 引擎軌 / "attack" 攻擊軌
    price: float
    change_rate: float
    aflow: int
    total_volume: int = 0
    ma20: Optional[float] = None
    trigger_price: Optional[float] = None   # 攻擊軌盤前定
    atr_stop: Optional[float] = None        # 攻擊軌盤前定
    inst_buy_days: int = 0                   # 昨日盤後值


def passes_filters(s: StockSnap, regime: Optional[str] = None) -> dict:
    """
    對一檔跑全部篩選條件，回傳逐條結果 + 是否全過。
    UI「符合條件」模式：all_pass=True 才顯示。
    """
    rules: List[Tuple[str, str, object]] = [
        ("aflow_positive", "主動差>0", st_aflow_positive),
        ("above_ma20", "站上MA20", st_above_ma20),
        ("aflow_intensity", "吸籌強度足", st_aflow_intensity),
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
        else:
            # no_data 是獨立狀態，不混進 failed。
            pass
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
        # 舊欄位保留，讓既有 API 呼叫不會直接崩潰。
        **checks,
    }


def rank_potential(snaps: List["StockSnap"]) -> List[dict]:
    """主排 aflow 強度，次排抗跌（漲跌幅較高者優先）。"""
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
# 四、雙軌狀態機轉移公式（v3.0 playbook）
#     引擎軌：站上 MA20 + 昨日法人連買 → 訊號成立；跌破 MA20 → 停損
#     攻擊軌：突破觸發價 → 訊號成立；跌破 ATR 停損 → 停損
# =====================================================================

STATES = ["觀察中", "訊號成立", "已進場", "停損觸發", "停利觸發", "出場"]


def engine_signal(s: StockSnap, min_inst_buy_days: int = 1) -> bool:
    """引擎軌訊號成立：站上月線 + 昨日法人連買 ≥ 門檻。"""
    return cond_above_ma20(s.price, s.ma20) and s.inst_buy_days >= min_inst_buy_days


def engine_stop(s: StockSnap) -> bool:
    """引擎軌停損：跌破月線。"""
    d = dist_ma20(s.price, s.ma20)
    return d is not None and d < 0


def attack_signal(s: StockSnap) -> bool:
    """攻擊軌訊號成立：突破當日觸發價。"""
    return s.trigger_price is not None and s.price >= s.trigger_price


def attack_stop(s: StockSnap) -> bool:
    """攻擊軌停損：跌破 ATR 停損價。"""
    return s.atr_stop is not None and s.price < s.atr_stop


def next_state(prev_state: str, s: StockSnap) -> str:
    """
    依軌道分流的狀態轉移。回傳新狀態；與 prev_state 不同時 → 呼叫端發事件。
    華邦電教訓：不可用引擎軌 swing 邏輯套攻擊軌，故嚴格依 track 分流。
    """
    is_engine = s.track == "engine"
    stopped = engine_stop(s) if is_engine else attack_stop(s)
    signaled = engine_signal(s) if is_engine else attack_signal(s)

    if prev_state in ("已進場", "訊號成立") and stopped:
        return "停損觸發"
    if prev_state == "觀察中" and signaled:
        return "訊號成立"
    return prev_state
