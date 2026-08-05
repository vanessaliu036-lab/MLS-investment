"""
signal_pattern.py — 昨日訊號「型態」分類 + 今日觸發原因(純函式,可單測)

兩個時點,兩個責任,不要混:

  ① classify(bars)         選股當下(盤後 T 日)用日K判「這檔為什麼入選」→ 6 種型態
                            回傳 signal_type / kind / trigger_price / evidence。
                            型態只描述「昨日(T)訊號長相」,不預測明日。

  ② describe_trigger(...)   T+1 收盤驗證時判「今日到底有沒有觸發原定進場條件」,
                            未觸發一律給【明確原因】(未突破昨高 X、今日最高 Y、差 Z%),
                            絕不回「原定進場條件未成立」這種廣泛語。

型態判定優先序(一檔可能同時符合多條,取最強一個顯示):
  W底 → 均線黃金交叉 → 放量長紅 → 突破前高 → 量縮止跌 → 回測月線

門檻集中在檔頭常數(使用者 2026-08-04 定案):
  放量=均量20×2、長紅=漲幅≥+3%、量縮=均量20×0.7、回測月線=MA20±1.5%

資料不足一律回 None / 空原因,不硬湊(對齊全系統三態鐵律)。
bars: broker.daily_kbars 回傳的日K list(舊→新),每筆 dict 含 close/high/low/open/volume。
"""

from __future__ import annotations

import indicators

# ── 型態判定門檻(使用者定案) ─────────────────────────────
VOL_SURGE = 2.0          # 放量:量 ≥ 均量20 × 此倍數
LONG_RED_PCT = 3.0       # 長紅:單日漲幅(%) ≥ 此值 且 收 > 開
VOL_SHRINK = 0.7         # 量縮:量 ≤ 均量20 × 此倍數
MA20_BAND = 0.015        # 回測月線:|收盤 − MA20| / MA20 ≤ 此比例(±1.5%)
BREAKOUT_LOOKBACK = 20   # 突破前高:回看天數(不含當日)
SHRINK_MIN_CHG = -1.0    # 量縮止跌:跌幅不深於此(%)才算「止跌」

# ── W 底參數 ──────────────────────────────────────────────
W_WINDOW = 20            # 型態掃描窗
W_LOW_TOL = 0.05         # 兩個低點需相近(相差 ≤ 5%)
W_NECK_MIN = 0.02        # 頸線需高於低點 ≥ 2%(否則不算像樣的 W)
W_MIN_GAP = 3            # 兩低點至少間隔幾根


BREAKOUT_TYPES = {"🔺 W底成型", "📊 均線黃金交叉", "🔥 放量長紅", "📈 突破前高"}
PULLBACK_TYPES = {"🧊 量縮止跌", "📉 回測月線"}


def default_trigger(bars, kind):
    """無明確型態時的預設觸發價,讓每一檔都有可判定的原定進場條件(不留『缺觸發價』)。
    breakout(radar/攻擊)→ 最後一根(選股日)最高價(＝明日昨高);pullback(resilient)→ MA20。"""
    if not bars:
        return None
    closes = [b.get("close") for b in bars if b.get("close") is not None]
    highs = [b.get("high") for b in bars if b.get("high") is not None]
    if kind == "pullback":
        ma20 = indicators.sma(closes, 20)
        v = ma20 if ma20 is not None else (highs[-1] if highs else None)
    else:
        v = highs[-1] if highs else None
    return round(v, 2) if v is not None else None


def kind_of(signal_type, source=None):
    """由型態(或退回 source)判進場性質:breakout(突破昨高) / pullback(回月線)。
    T+1 驗證只讀得到 signal_type,用這支還原 kind 給 describe_trigger。"""
    if signal_type in BREAKOUT_TYPES:
        return "breakout"
    if signal_type in PULLBACK_TYPES:
        return "pullback"
    if source == "resilient":
        return "pullback"
    if source == "radar":
        return "breakout"
    return None


def _series(bars):
    return (
        [b["close"] for b in bars],
        [b["high"] for b in bars],
        [b["low"] for b in bars],
        [b["open"] for b in bars],
        [b["volume"] for b in bars],
    )


def _golden_cross(closes):
    """MA5 由下上穿 MA10(前一日 MA5≤MA10、今日 MA5>MA10)。"""
    m5 = indicators.sma_series(closes, 5)
    m10 = indicators.sma_series(closes, 10)
    if len(m5) < 2 or len(m10) < 2:
        return None
    m5n, m5p, m10n, m10p = m5[-1], m5[-2], m10[-1], m10[-2]
    if m5p <= m10p and m5n > m10n:
        return f"MA5 上穿 MA10({m5p:.1f}/{m10p:.1f}→{m5n:.1f}/{m10n:.1f})"
    return None


def _detect_w_bottom(highs, lows, closes):
    """近 W_WINDOW 日兩個相近低點 + 中間頸線,今日收盤突破頸線 = W 成型。
    啟發式(broker 早期 low 曾以 close 補值,容差已放寬);資料不足回 None。"""
    if len(lows) < W_WINDOW:
        return None
    H, L, C = highs[-W_WINDOW:], lows[-W_WINDOW:], closes[-W_WINDOW:]
    n = len(L)
    # 只認「局部低點」(比左右鄰低)——單調漲/跌沒有內部谷,直接排除偽 W
    troughs = [i for i in range(1, n - 1) if L[i] <= L[i - 1] and L[i] <= L[i + 1]]
    if len(troughs) < 2:
        return None
    troughs.sort(key=lambda i: L[i])                       # 由低到高
    i1, i2 = sorted(troughs[:2])                           # 最低兩個谷,依時間排
    lo1, lo2 = L[i1], L[i2]
    if lo1 <= 0 or abs(lo2 - lo1) / lo1 > W_LOW_TOL:       # 兩底需相近
        return None
    a, b = i1, i2
    if b - a < W_MIN_GAP:
        return None
    neck = max(H[a:b + 1])                                 # 兩底之間的頸線
    if neck <= max(lo1, lo2) * (1 + W_NECK_MIN):           # 頸線不夠高 → 不像 W
        return None
    if C[-1] >= neck and (n - 1) > b:                      # 今日突破頸線且在右底之後
        return f"雙低 {lo1:.1f}/{lo2:.1f} 突破頸線 {neck:.1f}"
    return None


def classify(bars) -> dict:
    """回 {signal_type, kind, trigger_price, evidence}。
    kind: 'breakout'(攻擊型,明日觸發＝突破昨高) / 'pullback'(回測型,明日觸發＝回月線) / None。
    trigger_price: 明日進場的觸發價 —— 突破型＝今日(T)高(＝明日昨高)、回測型＝MA20。
    signal_type=None 表示無明確型態,呼叫端可退回 source/track 舊判定。"""
    empty = {"signal_type": None, "kind": None, "trigger_price": None, "evidence": "資料不足"}
    if not bars or len(bars) < 2:
        return empty
    closes, highs, lows, opens, vols = _series(bars)
    c, o, h = closes[-1], opens[-1], highs[-1]
    prev_close = closes[-2]
    chg = ((c / prev_close - 1) * 100) if prev_close else None
    ma20 = indicators.sma(closes, 20)
    vma20 = indicators.sma(vols, 20)
    vol = vols[-1]
    lb = highs[-(BREAKOUT_LOOKBACK + 1):-1] if len(highs) > 1 else []
    prior_high = max(lb) if lb else None

    def _out(stype, kind, evidence):
        tp = h if kind == "breakout" else (ma20 if kind == "pullback" else None)
        tp = round(tp, 2) if tp is not None else None
        return {"signal_type": stype, "kind": kind, "trigger_price": tp, "evidence": evidence}

    # ① W 底
    w = _detect_w_bottom(highs, lows, closes)
    if w:
        return _out("🔺 W底成型", "breakout", w)

    # ② 均線黃金交叉
    gc = _golden_cross(closes)
    if gc:
        return _out("📊 均線黃金交叉", "breakout", gc)

    # ③ 放量長紅
    if vma20 and vol >= vma20 * VOL_SURGE and c > o and chg is not None and chg >= LONG_RED_PCT:
        return _out("🔥 放量長紅", "breakout", f"量 {vol / vma20:.1f} 倍均量、收紅 {chg:+.1f}%")

    # ④ 突破前高
    if prior_high and c >= prior_high:
        return _out("📈 突破前高", "breakout", f"收 {c:.1f} ≥ 前高 {prior_high:.1f}")

    # ⑤ 量縮止跌
    if (vma20 and ma20 and vol <= vma20 * VOL_SHRINK and c >= ma20
            and chg is not None and chg >= SHRINK_MIN_CHG):
        return _out("🧊 量縮止跌", "pullback", f"量縮至 {vol / vma20:.1f} 倍、守月線 {ma20:.1f}")

    # ⑥ 回測月線
    if ma20 and abs(c - ma20) / ma20 <= MA20_BAND and c >= ma20 * (1 - MA20_BAND):
        return _out("📉 回測月線", "pullback", f"收 {c:.1f} 貼近月線 {ma20:.1f}")

    return empty


def describe_trigger(kind, trigger_price, today_high, today_low, today_close, chg,
                     volume_ratio=None, aflow=None, rel=None) -> tuple:
    """T+1 收盤:判「今日有沒有觸發原定進場條件」+ 未觸發的明確原因。
    回 (trigger_status, non_trigger_reason)。
      trigger_status ∈ 突破/未突破/止穩/未回測/—
      non_trigger_reason: 觸發時為 ''、未觸發時給明確原因(帶昨高/月線與差距)。
    缺資料時回 ('—', 具體缺什麼),不回廣泛語。"""

    def _pct(a, b):
        return (a / b - 1) * 100 if (a is not None and b) else None

    if trigger_price is None:
        return ("—", "缺原定觸發價,無法判定今日是否觸發")

    # ── 突破型:今日最高 ≥ 昨高 才算觸發 ──
    if kind == "breakout":
        if today_high is None:
            return ("—", f"缺今日最高價,無法判定是否突破昨高 {trigger_price}")
        if today_high >= trigger_price:
            return ("突破", "")
        gap = _pct(today_high, trigger_price)
        # 開頭帶「價格未達進場區」讓凍結的 UI 分類器命中正確標籤(而非落到
        # 「原定進場條件未成立」廣泛語);後半保留昨高/今高/差距明細供回測。
        reason = (f"價格未達進場區:未突破昨高 {trigger_price}(今日最高 {today_high}"
                  + (f",差 {gap:+.1f}%" if gap is not None else "") + ")")
        return ("未突破", _enrich(reason, chg, volume_ratio, aflow, rel))

    # ── 回測型:今日需回測到月線且守住 ──
    if kind == "pullback":
        touched = (today_low is not None and today_low <= trigger_price)
        held = (today_close is not None and today_close >= trigger_price)
        if touched and held:
            return ("止穩", "")
        if today_low is None:  # 缺最低價 → 用收盤近似:貼月線且守住視為止穩
            if held and today_close is not None and today_close <= trigger_price * (1 + MA20_BAND):
                return ("止穩", "")
        if today_low is not None and today_low > trigger_price:
            gap = _pct(today_low, trigger_price)
            reason = (f"未回測到月線 {trigger_price}(今日最低 {today_low}"
                      + (f",高於月線 {gap:+.1f}%" if gap is not None else "") + " 未觸及),無回測進場機會")
            return ("未回測", reason)
        if today_close is not None and today_close < trigger_price:
            gap = _pct(today_close, trigger_price)
            reason = (f"跌破月線 {trigger_price}(收 {today_close}"
                      + (f",{gap:+.1f}%" if gap is not None else "") + "),回測支撐失守")
            return ("未回測", reason)
        return ("未回測", f"未回測到月線 {trigger_price},無回測進場機會")

    return ("—", "此檔無明確型態的原定進場條件")


def _enrich(base, chg, volume_ratio, aflow, rel):
    """未觸發時,除了價格差距,補一句最主要的「為何沒動」佐證(資金/量能/相對族群)。"""
    extra = None
    if aflow is not None and aflow < 0:
        extra = "盤中資金流出"
    elif volume_ratio is not None and volume_ratio < 1:
        extra = f"量能不足(量比 {volume_ratio:.1f})"
    elif rel is not None and rel < 0:
        extra = f"弱於族群 {rel:+.1f}pp"
    return f"{base};{extra}" if extra else base


# ════════════════════════════════════════════════════════
# 自測:python signal_pattern.py
# ════════════════════════════════════════════════════════
if __name__ == "__main__":
    def bar(c, h, l, o, v):
        return {"close": c, "high": h, "low": l, "open": o, "volume": v}

    # 放量長紅
    hist = [bar(100 + i * 0.1, 100 + i * 0.1 + 1, 100 + i * 0.1 - 1, 100 + i * 0.1, 1000)
            for i in range(25)]
    hist.append(bar(108, 109, 101, 101, 3000))   # 收紅 +7.7%、量3倍
    r = classify(hist)
    assert r["signal_type"] == "🔥 放量長紅", r
    assert r["kind"] == "breakout" and r["trigger_price"] == 109, r
    print("① 放量長紅 OK:", r)

    # 突破前高(穩定緩升,黃金交叉早已發生 → 今日僅創新高,非長紅/非交叉)
    hist2 = [bar(100 + i * 0.5, 100 + i * 0.5 + 0.3, 100 + i * 0.5 - 0.3, 100 + i * 0.5, 1000)
             for i in range(26)]
    r2 = classify(hist2)
    assert r2["signal_type"] == "📈 突破前高", r2
    print("② 突破前高 OK:", r2)

    # 觸發判定:未突破昨高 → 明確原因
    st, why = describe_trigger("breakout", 36.45, today_high=36.10, today_low=35.5,
                               today_close=35.8, chg=-1.2, volume_ratio=0.6, aflow=None)
    assert st == "未突破" and "價格未達進場區" in why and "36.45" in why and "36.1" in why, (st, why)
    print("③ 未突破明確原因 OK:", why)

    # 觸發判定:已突破
    st2, why2 = describe_trigger("breakout", 36.45, today_high=37.0, today_low=36.0,
                                 today_close=36.9, chg=1.5)
    assert st2 == "突破" and why2 == "", (st2, why2)
    print("④ 突破觸發 OK")

    # 回測型未回測
    st3, why3 = describe_trigger("pullback", 50.0, today_high=53, today_low=51.5,
                                 today_close=52.8, chg=0.3)
    assert st3 == "未回測" and "50.0" in why3, (st3, why3)
    print("⑤ 未回測明確原因 OK:", why3)
    print("—— signal_pattern 自測全過 ——")
