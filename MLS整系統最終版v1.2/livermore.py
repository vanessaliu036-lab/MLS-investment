"""
MLS 插件 v10 — livermore.py
李佛摩 (Jesse Livermore) 關鍵點位篩選器
====================================================================
不動主系統,純從 STATE 與 broker.daily_kbars 讀資料。
六點轉向經典規則(本檔簡化版):
  1. 大盤趨勢判斷(MA20 vs MA60)
  2. 上漲趨勢:股價回測至 50% / 33% 關鍵回撤位 = 買點
  3. 下跌趨勢:反彈至 50% 關鍵反彈位 = 空點(本系統僅標警示,不出空單)
  4. 自然回撤位 (NR) 計算: 高點 - (高點-低點)*[0.33, 0.50, 0.66]
  5. 突破新高 + 量增 = 趨勢確認 (新買點)
  6. 跌破前低 + 量增 = 趨勢反轉 (賣出)

API:
  - /api/livermore             全觀察池李佛摩篩選
  - /api/livermore/{code}      單一個股詳細分析
"""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("livermore")


def _safe(v):
    if v is None:
        return None
    if isinstance(v, float):
        return None if v != v else v  # NaN guard
    return v


def _last_high_low(kbars: List[Dict], n: int = 60) -> Dict[str, Any]:
    """取近 n 根 K 棒的最高/最低/收盤。
    broker.daily_kbars 不給 low 鍵,fallback 用 close 與 (high-close) 5% buffer 估區間下限。
    """
    closes = [k.get("close") for k in kbars if k.get("close") is not None]
    highs = [k.get("high") for k in kbars if k.get("high") is not None]
    lows_raw = [k.get("low") for k in kbars if k.get("low") is not None]
    if not closes or not highs:
        return {"high": None, "low": None, "close": None, "pct_range": None}
    win_high = max(highs)
    if lows_raw:
        win_low = min(lows_raw)
    else:
        # fallback:用每根 (high - 0.05*high) 作為該根估低,取最小
        est_lows = [c * 0.95 for c in closes]
        win_low = min(est_lows) if est_lows else min(closes)
    rng = win_high - win_low
    pct_range = (rng / win_low * 100) if win_low else 0
    return {
        "high": win_high,
        "low": win_low,
        "close": closes[-1],
        "prev_close": closes[-2] if len(closes) >= 2 else None,
        "pct_range": round(pct_range, 2),
    }


def _ma(closes: List[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def analyze_stock(snap: Dict[str, Any], kbars: List[Dict]) -> Dict[str, Any]:
    """
    對單檔個股跑李佛摩六點分析。
    回傳 dict 含:trend / signal / key_levels / pivot_points / commentary
    """
    out = {
        "code": snap.get("code"),
        "name": snap.get("name"),
        "sector": snap.get("sector"),
        "price": snap.get("price"),
        "change_rate": snap.get("change_rate"),
        "trend": "sideways",
        "signal": "neutral",
        "signal_strength": 0,
        "key_levels": {},
        "pivot_points": {},
        "commentary": [],
    }

    closes = [k.get("close") for k in kbars if k.get("close") is not None]
    highs = [k.get("high") for k in kbars if k.get("high") is not None]
    lows = [k.get("low") for k in kbars if k.get("low") is not None]
    vols = [k.get("volume", 0) for k in kbars if k.get("volume") is not None]
    if len(closes) < 20:
        out["commentary"].append("K 棒不足 20 根,無法計算關鍵點位")
        return out

    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60) if len(closes) >= 60 else None
    price = closes[-1]

    # 1. 趨勢判斷
    if ma60 and price > ma60 and ma20 and ma20 > ma60:
        trend = "uptrend"
    elif ma60 and price < ma60 and ma20 and ma20 < ma60:
        trend = "downtrend"
    elif ma20 and price > ma20:
        trend = "uptrend_weak"
    elif ma20 and price < ma20:
        trend = "downtrend_weak"
    else:
        trend = "sideways"
    out["trend"] = trend

    # 2-4. 計算 60 根/30 根區間的高低點與自然回撤位
    hl60 = _last_high_low(kbars, 60)
    hl30 = _last_high_low(kbars[-30:] if len(kbars) >= 30 else kbars, 30)
    hi, lo = hl60["high"], hl60["low"]
    rng = hi - lo if (hi is not None and lo is not None) else 0

    pivots = {}
    if rng > 0:
        pivots["NR_33"] = round(hi - rng * 0.33, 2)   # 33% 回撤(淺)
        pivots["NR_50"] = round(hi - rng * 0.50, 2)   # 50% 回撤(中)
        pivots["NR_66"] = round(hi - rng * 0.66, 2)   # 66% 回撤(深)
        pivots["R_50"]  = round(lo + rng * 0.50, 2)   # 下跌中段反彈位
    out["pivot_points"] = pivots
    out["key_levels"] = {
        "60d_high": hi,
        "60d_low": lo,
        "30d_high": hl30["high"],
        "30d_low": hl30["low"],
        "pct_range_60d": hl60["pct_range"],
    }

    # 量能(近 5 日均 vs 近 20 日均)→ 突破/跌破需量增
    avg5v = _ma(vols, 5)
    avg20v = _ma(vols, 20)
    vol_expansion = (avg5v / avg20v) if (avg5v and avg20v) else 1.0

    # 5. 突破新高 / 跌破前低
    prev_close = closes[-2] if len(closes) >= 2 else None
    near_high = (hi is not None and price >= hi * 0.995)
    near_low = (lo is not None and price <= lo * 1.005)
    breakout_new_high = (prev_close is not None and hi is not None
                          and price > hi and prev_close <= hi)
    breakdown_new_low = (prev_close is not None and lo is not None
                          and price < lo and prev_close >= lo)

    signal = "neutral"
    strength = 0
    commentary = []
    neutral_reason = None  # 給前端顯示「為什麼沒訊號」

    if trend in ("uptrend", "uptrend_weak"):
        # 上漲趨勢:回測 NR_50 = 加碼買點
        if pivots.get("NR_50") and abs(price - pivots["NR_50"]) / pivots["NR_50"] < 0.02:
            signal = "buy_pullback"
            strength = 70
            commentary.append(f"回測 50% 自然回撤位 ${pivots['NR_50']},李佛摩加碼區")
        elif pivots.get("NR_33") and abs(price - pivots["NR_33"]) / pivots["NR_33"] < 0.02:
            signal = "buy_shallow_pullback"
            strength = 60
            commentary.append(f"回測 33% 淺回撤 ${pivots['NR_33']},強勢上漲健康回調")
        elif breakout_new_high and vol_expansion > 1.2:
            signal = "buy_breakout"
            strength = 85
            commentary.append(f"突破 60 日新高 ${hi},量比 {vol_expansion:.2f} 確認")
        elif near_high and vol_expansion > 1.0:
            signal = "buy_near_breakout"
            strength = 55
            commentary.append(f"接近 60 日新高 ${hi}({price}),量能溫和")

    elif trend in ("downtrend", "downtrend_weak"):
        # 下跌趨勢:僅標警示,不出空單
        if pivots.get("R_50") and abs(price - pivots["R_50"]) / pivots["R_50"] < 0.02:
            signal = "watch_bounce_fail"
            strength = -50
            commentary.append(f"反彈至 50% 中段 ${pivots['R_50']},下跌趨勢中段(僅警示)")
        elif breakdown_new_low and vol_expansion > 1.2:
            signal = "sell_breakdown"
            strength = -85
            commentary.append(f"跌破 60 日新低 ${lo},量增確認弱勢")
        elif near_low:
            signal = "watch_near_low"
            strength = -40
            commentary.append(f"接近 60 日新低 ${lo},不接刀")

    else:
        # 盤整:突破跟跌破都觀望
        if breakout_new_high and vol_expansion > 1.3:
            signal = "watch_breakout"
            strength = 30
            commentary.append(f"盤整突破 60 日高 ${hi},量比 {vol_expansion:.2f},待確認趨勢翻多")
        elif breakdown_new_low and vol_expansion > 1.3:
            signal = "watch_breakdown"
            strength = -30
            commentary.append(f"盤整跌破 60 日低 ${lo},量比 {vol_expansion:.2f},待確認趨勢翻空")

    out["signal"] = signal
    out["signal_strength"] = strength
    out["vol_expansion"] = round(vol_expansion, 2)
    out["ma20"] = ma20
    out["ma60"] = ma60

    # neutral 強化說明:沒觸發 ≠ 沒資料,要給使用者明確原因
    if signal == "neutral" and not commentary:
        if not kbars or len(kbars) < 20:
            neutral_reason = "K 棒資料不足 20 根,無法計算回撤/突破"
        else:
            # 根據趨勢跟點位距離判斷「為什麼沒訊號」
            dist_to_pivot = None
            nearest_pivot_name = None
            if pivots:
                # 找最近的回撤位
                dists = []
                for name, level in pivots.items():
                    if level and name.startswith("NR_"):
                        d = abs(price - level) / level * 100
                        dists.append((d, name, level))
                if dists:
                    dist_to_pivot, nearest_pivot_name, nearest_level = min(dists)
            if trend in ("uptrend", "uptrend_weak"):
                if dist_to_pivot is not None and dist_to_pivot > 2:
                    neutral_reason = (f"上升趨勢中,離 NR_{nearest_pivot_name.split('_')[1]} 回撤位 {nearest_level} 還有 {dist_to_pivot:.1f}% 距離 "
                                       f"(容忍 2%)。要等股價回測才能進場")
                else:
                    neutral_reason = "上升趨勢中但未接近回撤位、也未突破 60 日高(無明確買點)"
            elif trend in ("downtrend", "downtrend_weak"):
                neutral_reason = "下跌趨勢中,本系統只標示警示不出進場訊號(避開接刀)"
            else:
                neutral_reason = "盤整格局,未觸發突破(>60 日高+量增)也未觸發跌破(>60 日低+量增)"
        commentary = [neutral_reason]
    elif signal == "neutral" and commentary:
        # 已有 commentary 但中性 → 補一行「沒觸發」原因
        commentary.append("目前未觸發李佛摩六點任何買/賣訊號")

    out["commentary"] = commentary
    out["neutral_reason"] = neutral_reason
    return out


def screen_pool(state_stocks: List[Dict], get_kbars) -> List[Dict]:
    """
    跑全觀察池李佛摩篩選,回傳排序後的清單。
    get_kbars: callable(code) -> list of kbars(用 lazy 載入,免爆 broker)
    """
    rows = []
    for s in (state_stocks or []):
        code = s.get("code")
        if not code:
            continue
        try:
            kbars = get_kbars(code) or []
        except Exception as e:
            log.warning(f"livermore kbars {code} 失敗:{e}")
            kbars = []
        result = analyze_stock(s, kbars)
        rows.append(result)
    # 排序:買訊優先(強度高到低),再來是警示
    rows.sort(key=lambda r: r.get("signal_strength", 0), reverse=True)
    return rows
