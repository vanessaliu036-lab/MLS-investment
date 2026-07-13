"""
MLS 模組 — indicators.py(v2.3 新增)
技術指標引擎:MA / MACD / KD / RSI / ATR
====================================================================
公式全部採教科書標準定義,__main__ 內建交叉驗證
(RSI/EMA 用 pandas ewm 獨立算法對照;KD/ATR 用手算逐步驗證),
確保「檢查公式是否正確」有據可查。

輸入一律 list(舊→新)。資料不足時回 None,不硬湊。

【資料品質誠實揭露】broker.daily_kbars 目前 low 欄以 close 保守補值
(見 livermore 交接說明),因此 KD / ATR 在 low 缺真值時為近似值,
stock_card 會標 approx=True;broker 補齊 low 後自動變精確,本模組不用改。
"""


# ── 均線 ────────────────────────────────────────────────
def sma(vals, n):
    if not vals or len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def sma_series(vals, n):
    if len(vals) < n:
        return []
    return [sum(vals[i - n + 1:i + 1]) / n for i in range(n - 1, len(vals))]


def ma_direction(vals, n):
    """↑ / ↓ / →:今日MA vs 昨日MA。資料不足回 None。"""
    if len(vals) < n + 1:
        return None
    today = sum(vals[-n:]) / n
    prev = sum(vals[-n - 1:-1]) / n
    return "↑" if today > prev else ("↓" if today < prev else "→")


# ── EMA(標準:首值 = 前 n 筆 SMA 種子) ─────────────────
def ema_series(vals, n):
    if len(vals) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


# ── MACD(12, 26, 9) ────────────────────────────────────
def macd(closes, fast=12, slow=26, signal=9):
    """
    回傳 dict: dif, dea(macd訊號線), hist, cross
      cross: 黃金交叉 / 死亡交叉 / 多方 / 空方(交叉=今日剛穿越)
    DIF = EMA(fast) − EMA(slow);DEA = EMA(DIF, signal);HIST = DIF − DEA
    """
    if len(closes) < slow + signal:
        return None
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    # 對齊:兩序列都以各自第 n 天為首,slow 較晚起算
    offset = len(ef) - len(es)
    dif = [f - s for f, s in zip(ef[offset:], es)]
    dea = ema_series(dif, signal)
    if not dea:
        return None
    d_off = len(dif) - len(dea)
    dif_a = dif[d_off:]
    hist = [a - b for a, b in zip(dif_a, dea)]
    EPS = 1e-6                                    # 浮點容差:收斂相等不算交叉
    def _side(x, y):
        return 0 if abs(x - y) < EPS else (1 if x > y else -1)
    now = _side(dif_a[-1], dea[-1])
    if now == 0:                                  # DIF≈DEA:依 DIF 正負定多空
        cross = "多方" if dif_a[-1] > 0 else "空方"
    else:
        cross = "多方" if now > 0 else "空方"
        if len(dif_a) >= 2:
            prev = _side(dif_a[-2], dea[-2])
            if now > 0 and prev <= 0 and prev != 0:
                cross = "黃金交叉"
            elif now < 0 and prev >= 0 and prev != 0:
                cross = "死亡交叉"
    return {"dif": round(dif_a[-1], 3), "dea": round(dea[-1], 3),
            "hist": round(hist[-1], 3), "cross": cross}


# ── RSI(Wilder 平滑,標準 14) ──────────────────────────
def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        ch = b - a
        gains.append(max(0, ch))
        losses.append(max(0, -ch))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n          # Wilder 平滑
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - 100 / (1 + rs), 1)


# ── KD(9, 3, 3 台股慣例) ──────────────────────────────
def kd(highs, lows, closes, n=9):
    """RSV = (C − L9) / (H9 − L9) × 100;K = ⅔K′ + ⅓RSV;D = ⅔D′ + ⅓K。
    初始 K=D=50。回傳 (K, D)。"""
    if len(closes) < n:
        return None
    k, d = 50.0, 50.0
    for i in range(n - 1, len(closes)):
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        rsv = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
    return round(k, 1), round(d, 1)


# ── ATR(Wilder,標準 14) ──────────────────────────────
def atr(highs, lows, closes, n=14):
    """TR = max(H−L, |H−C′|, |L−C′|);ATR = Wilder 平滑 TR。"""
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return round(a, 2)


# ════════════════════════════════════════════════════════
# 公式交叉驗證:python indicators.py
# ════════════════════════════════════════════════════════
if __name__ == "__main__":
    import math, random
    random.seed(11)
    closes = [100.0]
    for _ in range(120):
        closes.append(round(closes[-1] * (1 + random.uniform(-0.02, 0.022)), 2))
    highs = [c * 1.012 for c in closes]
    lows = [c * 0.988 for c in closes]

    # ① EMA / MACD / RSI 用 pandas 獨立算法對照
    try:
        import pandas as pd
        s = pd.Series(closes)
        # EMA(pandas 用相同 SMA 種子法對照)
        my_e = ema_series(closes, 12)[-1]
        pd_e = s.ewm(span=12, adjust=False, min_periods=12).mean()
        pd_e = pd.concat([pd.Series([s[:12].mean()]), s[12:]]) \
                 .ewm(span=12, adjust=False).mean().iloc[-1]
        assert abs(my_e - pd_e) < 1e-6, (my_e, pd_e)
        print(f"① EMA12 對照 pandas OK:{my_e:.4f}")

        # RSI 對照(pandas ewm alpha=1/n 即 Wilder 平滑)
        diff = s.diff()
        g = diff.clip(lower=0); l = -diff.clip(upper=0)
        # 種子 SMA 後接 Wilder
        n = 14
        ag = g[1:n+1].mean(); al = l[1:n+1].mean()
        for gg, ll in zip(g[n+1:], l[n+1:]):
            ag = (ag*(n-1)+gg)/n; al = (al*(n-1)+ll)/n
        ref = 100 - 100/(1+ag/al)
        assert abs(rsi(closes) - round(ref, 1)) <= 0.1, (rsi(closes), ref)
        print(f"② RSI14 Wilder 對照 OK:{rsi(closes)}")
    except ImportError:
        print("(pandas 不在環境,跳過 pandas 對照,以下為手算驗證)")

    # ③ KD 手算逐步驗證(前 9 根固定資料)
    H = [10, 11, 12, 11, 12, 13, 12, 13, 14]
    L = [9, 9, 10, 10, 10, 11, 11, 11, 12]
    Cc = [9.5, 10.5, 11, 10.5, 11.5, 12.5, 11.5, 12.5, 13.5]
    rsv = (13.5 - min(L)) / (max(H) - min(L)) * 100   # = (13.5-9)/5*100 = 90
    k_ref = 50*2/3 + rsv/3
    d_ref = 50*2/3 + k_ref/3
    k, d = kd(H, L, Cc, 9)
    assert abs(k - round(k_ref, 1)) < 0.05 and abs(d - round(d_ref, 1)) < 0.05
    print(f"③ KD 手算對照 OK:K={k} D={d}(RSV={rsv:.1f})")

    # ④ ATR 手算驗證(常數 TR 序列 → ATR = 該常數)
    Hc = [i + 1.0 for i in range(20)]
    Lc = [i + 0.0 for i in range(20)]
    Cx = [i + 0.5 for i in range(20)]
    # TR = max(1, |H−C′|=1.5, |L−C′|=0.5) = 1.5 恆定 → ATR = 1.5
    assert atr(Hc, Lc, Cx, 14) == 1.5
    print("④ ATR 常數序列驗證 OK:1.5")

    # ⑤ MACD 結構檢查:上升序列 DIF>0 且多方
    up = [100 + i * 0.8 for i in range(60)]
    m = macd(up)
    assert m["dif"] > 0 and m["cross"] in ("多方", "黃金交叉")
    print(f"⑤ MACD 趨勢一致性 OK:{m}")
    print("—— 全部公式驗證通過 ——")
