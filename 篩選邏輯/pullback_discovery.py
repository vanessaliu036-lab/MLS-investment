"""pullback_discovery.py — Healthy Pullback Entry Framework，公式凍結版

研究過程與判定見 winning_model_backtest/FROZEN_HEALTHY_PULLBACK_V1.md
（2026-08-26 定案）。這支只做一件事:把凍結公式重複套用在新的交易日，
不再重新定義 peak/trough/reclaim、不再搜尋 threshold。

狀態:DISCOVERY ONLY。不進 UI、不影響 ENTER/WAIT、不影響排序 —— 純觀察，
供 20 個新獨立交易日 interim read、30 個新獨立交易日正式 re-test 用。

State machine:IMPULSE → PULLBACK → RECLAIMED，因果、逐格往前掃，
不用全日最高/最低回填(那是 hindsight，不是可交易的判斷)。
"""
from __future__ import annotations

import sqlite3

RULE_VERSION = "healthy_pullback_v1_2026-08-26"

# 這兩個是「偵測雜訊」用的門檻(判斷是不是一次真的拉抬/真的回撤)，
# 不是分級門檻，不跟 flow_retention/volume_contraction 的分級數字混淆，不再調。
PULLBACK_TRIGGER = 0.003   # 從高點回落超過 0.3% 才算進入 PULLBACK 狀態
RECLAIM_TRIGGER = 0.003    # 從低點反彈超過 0.3% 且 net_active 同步回升才算 RECLAIMED
STALE_RUN_LEN = 3          # net_active 連續 N 格完全不變 → freshness 疑慮 proxy

HORIZONS = {"h15m": 3, "h30m": 6, "h60m": 12}  # 5 分鐘一格


def find_limitup_events(db_path: str = "mls.db") -> list[dict]:
    """真鎖漲停(收盤=當日最高,非衝高拉回)且已知下一交易日的事件。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        bars = conn.execute(
            "SELECT code,data_date,open,high,low,close,volume FROM daily_bar "
            "ORDER BY code,data_date"
        ).fetchall()
    finally:
        conn.close()

    by_code: dict[str, list[dict]] = {}
    for r in bars:
        by_code.setdefault(r["code"], []).append(dict(r))

    events = []
    for code, series in by_code.items():
        for i in range(1, len(series)):
            prev, cur = series[i - 1], series[i]
            if not (prev["close"] and cur["close"] and prev["close"] > 0):
                continue
            chg = cur["close"] / prev["close"] - 1
            if chg < 0.095 or cur["high"] is None or abs(cur["high"] - cur["close"]) > 1e-6:
                continue
            if i + 1 >= len(series):
                continue
            d1 = series[i + 1]
            d2 = series[i + 2] if i + 2 < len(series) else None
            events.append({
                "code": code, "limitup_date": cur["data_date"], "limitup_close": cur["close"],
                "d1_date": d1["data_date"], "d1_close": d1["close"],
                "d2_date": d2["data_date"] if d2 else None,
                "d2_close": d2["close"] if d2 else None,
            })
    return events


def compute_case(event: dict, slots: list[dict]) -> dict | None:
    """套凍結公式在一組 b_snapshot 列(同一 code/d1_date，依 slot 升冪)。

    slots 每列需含 price/volume(累積量)/net_active。回傳 None 代表資料不夠
    (格數太少或缺現價)，呼叫端應該跳過、等下次資料補齊再算，不能半算硬寫。
    """
    if len(slots) < 10:
        return None
    prices = [s["price"] for s in slots]
    cvol = [s["volume"] for s in slots]
    flows = [s["net_active"] for s in slots]
    if any(p is None for p in prices):
        return None
    n = len(prices)

    state = "IMPULSE"
    peak_idx, peak_price = 0, prices[0]
    trough_idx, trough_price = None, None
    reclaim_idx = None
    for i in range(1, n):
        if state == "IMPULSE":
            if prices[i] >= peak_price:
                peak_price, peak_idx = prices[i], i
            elif prices[i] <= peak_price * (1 - PULLBACK_TRIGGER):
                state = "PULLBACK"
                trough_idx, trough_price = i, prices[i]
        elif state == "PULLBACK":
            if prices[i] < trough_price:
                trough_idx, trough_price = i, prices[i]
            f_i, f_trough = flows[i], flows[trough_idx]
            if (prices[i] >= trough_price * (1 + RECLAIM_TRIGGER)
                    and f_i is not None and f_trough is not None and f_i > f_trough):
                state = "RECLAIMED"
                reclaim_idx = i
                break

    rec = {
        "code": event["code"], "limitup_date": event["limitup_date"],
        "limitup_close": event["limitup_close"], "d1_date": event["d1_date"],
        "d1_close": event["d1_close"], "d2_date": event.get("d2_date"),
        "d2_close": event.get("d2_close"), "n_slots": n, "classification": state,
        "peak_idx": peak_idx, "peak_price": peak_price,
        "rule_version": RULE_VERSION,
    }
    if state == "IMPULSE":
        return rec  # 全日續創高，沒有回撤可觀察

    rec["trough_idx"] = trough_idx
    rec["trough_price"] = trough_price
    rec["pullback_depth"] = (peak_price - trough_price) / peak_price if peak_price else None

    f_peak, f_trough = flows[peak_idx], flows[trough_idx]
    rec["flow_retention"] = (f_trough / f_peak) if (f_peak is not None and f_trough is not None and f_peak > 0) else None

    span_end = reclaim_idx if reclaim_idx is not None else n - 1
    span = [flows[i] for i in range(peak_idx, span_end + 1) if flows[i] is not None]
    stale, run = False, 1
    for i in range(1, len(span)):
        if span[i] == span[i - 1]:
            run += 1
            if run >= STALE_RUN_LEN:
                stale = True
                break
        else:
            run = 1
    rec["flow_possibly_stale"] = stale

    impulse_minutes = peak_idx * 5
    impulse_rate = ((cvol[peak_idx] - cvol[0]) / impulse_minutes
                     if (impulse_minutes > 0 and cvol[0] is not None and cvol[peak_idx] is not None) else None)
    pullback_minutes = (trough_idx - peak_idx) * 5
    pullback_rate = ((cvol[trough_idx] - cvol[peak_idx]) / pullback_minutes
                      if (pullback_minutes > 0 and cvol[peak_idx] is not None and cvol[trough_idx] is not None) else None)
    rec["impulse_rate_per_min"] = impulse_rate
    rec["pullback_rate_per_min"] = pullback_rate
    rec["volume_contraction"] = (pullback_rate / impulse_rate) if (impulse_rate and impulse_rate > 0 and pullback_rate is not None) else None

    rec["support_hold"] = trough_price >= event["limitup_close"]

    if state != "RECLAIMED":
        return rec  # PULLBACK:回撤後這一日盤中沒有 reclaim，無 entry anchor

    entry_price = prices[reclaim_idx]
    rec["reclaim_idx"] = reclaim_idx
    rec["entry_price"] = entry_price
    for label, h in HORIZONS.items():
        end = min(reclaim_idx + h, n - 1)
        window = prices[reclaim_idx:end + 1]
        rec[f"mfe_{label}"] = max(window) / entry_price - 1
        rec[f"mae_{label}"] = min(window) / entry_price - 1
        rec[f"net_{label}"] = window[-1] / entry_price - 1
    window = prices[reclaim_idx:]
    rec["mfe_close"] = max(window) / entry_price - 1
    rec["mae_close"] = min(window) / entry_price - 1
    rec["net_close"] = window[-1] / entry_price - 1
    return rec
