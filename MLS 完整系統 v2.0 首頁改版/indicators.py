"""
MLS 標準版 — indicators.py
技術指標純函式庫:MA / MACD / KD / RSI / ATR
輸入:kbars list[dict{date, close, high, low, open, volume}]
輸出:dict 帶最新一筆指標值 + 各指標趨勢(↑/↓/→)
"""

from typing import Iterable


def _series(kbars, key):
    """從 kbars 抽欄位,過濾 None。"""
    return [k.get(key) for k in kbars if k.get(key) is not None]


def sma(values: Iterable[float], n: int):
    """簡單移動平均。回傳 list 對齊輸入(前 n-1 筆為 None)。"""
    vals = list(values)
    out = [None] * len(vals)
    if n <= 0:
        return out
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = round(s / n, 2)
    return out


def ema(values: Iterable[float], n: int):
    """指數移動平均。回傳 list,前 n-1 筆 None。"""
    vals = list(values)
    out = [None] * len(vals)
    if n <= 0 or not vals:
        return out
    k = 2 / (n + 1)
    # 種子:前 n 筆的 SMA
    if len(vals) < n:
        return out
    seed = sum(vals[:n]) / n
    out[n - 1] = round(seed, 2)
    prev = seed
    for i in range(n, len(vals)):
        prev = (vals[i] - prev) * k + prev
        out[i] = round(prev, 4)
    return out


def macd(closes: Iterable[float], fast=12, slow=26, signal=9):
    """
    MACD = EMA(fast) - EMA(slow);signal = EMA(MACD, signal);hist = MACD - signal
    回傳 (macd_line, signal_line, hist) 三條對齊 closes 的 list(前端 None)。
    """
    c = list(closes)
    ema_fast = ema(c, fast)
    ema_slow = ema(c, slow)
    macd_line = [None] * len(c)
    for i in range(len(c)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = round(ema_fast[i] - ema_slow[i], 4)
    # signal 用 macd_line 已 valid 段算 EMA
    sig_full = ema([v for v in macd_line if v is not None], signal)
    # 對齊回原長度
    sig_line = [None] * len(c)
    # 找到 macd 第一個非 None 位置
    first_idx = next((i for i, v in enumerate(macd_line) if v is not None), None)
    if first_idx is not None:
        for j, v in enumerate(sig_full):
            sig_line[first_idx + j] = v
    hist = [None] * len(c)
    for i in range(len(c)):
        if macd_line[i] is not None and sig_line[i] is not None:
            hist[i] = round(macd_line[i] - sig_line[i], 4)
    return macd_line, sig_line, hist


def rsi(closes: Iterable[float], n: int = 14):
    """RSI (Wilder 平滑)。前 n 筆 None。"""
    c = list(closes)
    out = [None] * len(c)
    if len(c) <= n:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        diff = c[i] - c[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_g = gains / n
    avg_l = losses / n
    rs = avg_g / avg_l if avg_l > 0 else float('inf')
    out[n] = round(100 - (100 / (1 + rs)), 2) if rs != float('inf') else 100.0
    prev_g, prev_l = avg_g, avg_l
    for i in range(n + 1, len(c)):
        diff = c[i] - c[i - 1]
        g = diff if diff > 0 else 0
        l = -diff if diff < 0 else 0
        prev_g = (prev_g * (n - 1) + g) / n
        prev_l = (prev_l * (n - 1) + l) / n
        rs = prev_g / prev_l if prev_l > 0 else float('inf')
        out[i] = round(100 - (100 / (1 + rs)), 2) if rs != float('inf') else 100.0
    return out


def kd(highs, lows, closes, n: int = 9, k_smooth: int = 3, d_smooth: int = 3):
    """
    隨機指標 KD:K=RSV 的 k_smooth SMA;D=K 的 d_smooth SMA。
    回傳 (k_vals, d_vals) 對齊輸入。
    """
    h = list(highs)
    l = list(lows)
    c = list(closes)
    rsv = [None] * len(c)
    for i in range(len(c)):
        if i < n - 1:
            continue
        hh = max(h[i - n + 1:i + 1])
        ll = min(l[i - n + 1:i + 1])
        if hh == ll:
            rsv[i] = 50.0
        else:
            rsv[i] = round((c[i] - ll) / (hh - ll) * 100, 2)
    # K = SMA(RSV, k_smooth)
    k_vals = sma([v for v in rsv if v is not None], k_smooth)
    # 對齊回原長度
    k_full = [None] * len(c)
    first_idx = next((i for i, v in enumerate(rsv) if v is not None), None)
    if first_idx is not None:
        for j, v in enumerate(k_vals):
            k_full[first_idx + j] = v
    d_vals = sma([v for v in k_full if v is not None], d_smooth)
    d_full = [None] * len(c)
    if first_idx is not None:
        # d 的有效起點是 k_full 再加 d_smooth-1
        d_start = first_idx + (k_smooth - 1) + (d_smooth - 1)
        for j, v in enumerate(d_vals):
            idx = d_start + j
            if 0 <= idx < len(c):
                d_full[idx] = v
    return k_full, d_full


def atr(highs, lows, closes, n: int = 14):
    """Average True Range (Wilder 平滑)。"""
    h = list(highs)
    l = list(lows)
    c = list(closes)
    if len(c) < 2:
        return [None] * len(c)
    tr = [None] * len(c)
    tr[0] = h[0] - l[0]
    for i in range(1, len(c)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    out = [None] * len(c)
    if len(c) < n:
        return out
    first = sum(tr[:n]) / n
    out[n - 1] = round(first, 4)
    prev = first
    for i in range(n, len(c)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = round(prev, 4)
    return out


def trend_arrow(curr, prev):
    """依最近兩點給方向箭頭:↑ / ↓ / →。"""
    if curr is None or prev is None:
        return "—"
    if curr > prev:
        return "↑"
    if curr < prev:
        return "↓"
    return "→"


def macd_state(hist_curr, hist_prev):
    """依 hist 變化給 MACD 狀態:黃金交叉 / 死亡交叉 / 多頭 / 空頭。"""
    if hist_curr is None or hist_prev is None:
        return "—"
    if hist_prev <= 0 and hist_curr > 0:
        return "黃金交叉"
    if hist_prev >= 0 and hist_curr < 0:
        return "死亡交叉"
    return "多頭" if hist_curr > 0 else "空頭"


def compute_all(kbars):
    """
    主入口:從 kbars 算所有技術指標。
    回傳 dict 含最新值 + 對齊每根 K 的序列(給 sparkline)。
    """
    closes = _series(kbars, "close")
    highs = _series(kbars, "high")
    lows = _series(kbars, "low")
    if len(closes) < 5:
        return {"ok": False, "reason": f"kbars 不足 ({len(closes)} 筆)"}
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    macd_line, sig_line, hist = macd(closes)
    rsi_vals = rsi(closes)
    k_vals, d_vals = kd(highs, lows, closes)
    atr_vals = atr(highs, lows, closes)

    last = len(closes) - 1
    prev = last - 1

    return {
        "ok": True,
        "ma5": ma5[last], "ma5_dir": trend_arrow(ma5[last], ma5[prev] if prev >= 0 else None),
        "ma10": ma10[last], "ma10_dir": trend_arrow(ma10[last], ma10[prev] if prev >= 0 else None),
        "ma20": ma20[last], "ma20_dir": trend_arrow(ma20[last], ma20[prev] if prev >= 0 else None),
        "macd": round(macd_line[last], 4) if macd_line[last] is not None else None,
        "macd_signal": round(sig_line[last], 4) if sig_line[last] is not None else None,
        "macd_hist": round(hist[last], 4) if hist[last] is not None else None,
        "macd_state": macd_state(hist[last], hist[prev] if prev >= 0 else None),
        "kd_k": round(k_vals[last], 2) if k_vals[last] is not None else None,
        "kd_d": round(d_vals[last], 2) if d_vals[last] is not None else None,
        "rsi": rsi_vals[last],
        "atr": atr_vals[last],
        # 給前端 sparkline 用(全序列)
        "_series": {
            "ma5": ma5, "ma10": ma10, "ma20": ma20,
            "macd": macd_line, "kd_k": k_vals, "kd_d": d_vals,
        },
    }