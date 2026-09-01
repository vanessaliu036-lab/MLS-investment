"""Research-only Extreme Outflow -> Day-1 Flow Reversal classifier.

This module is deliberately independent from the main MLS action engine.
It can identify early reversal and negative control states, but never grants
trading authority.
"""
from __future__ import annotations


def classify_reversal(
    *,
    prior_outflow: bool,
    extreme_outflow: bool,
    price_reversal: bool,
    aflow_flip: bool,
    aflow_persistence: bool,
    price_confirmation: bool,
    above_vwap: bool,
    stale_price: bool,
    five_day_flow_positive: bool = False,
    twenty_day_flow_negative: bool = False,
    price_weak: bool = False,
) -> str:
    """Return the research state for the separate reversal warning track.

    The early state intentionally does not wait 30-90 minutes for persistence.
    Two negative-control states are also explicit:
    - PREVIOUS_FLOW_REVERSAL_DAY2_3_FAILURE: long-window outflow but recent 5D
      already turned positive before today; current price then fails below VWAP.
    - OUTFLOW_WATCH_NOT_TRIGGERED: outflow background remains, but today never
      establishes a Day-1 price/VWAP reversal.
    """
    if stale_price:
        return "STALE_PRICE_DATA"

    if twenty_day_flow_negative and five_day_flow_positive and price_weak and not above_vwap:
        return "PREVIOUS_FLOW_REVERSAL_DAY2_3_FAILURE"

    if prior_outflow and price_weak and not price_reversal and not above_vwap:
        return "OUTFLOW_WATCH_NOT_TRIGGERED"

    if not prior_outflow:
        return "NOT_REVERSAL"

    if price_reversal and aflow_flip and above_vwap:
        if aflow_persistence and price_confirmation:
            return "REVERSAL_PRIORITY"
        return "REVERSAL_DAY1_EARLY"

    return "OUTFLOW_REVERSAL_WATCH"


def reversal_summary(state: str) -> str:
    if state == "STALE_PRICE_DATA":
        return "價格資料不是當日資料，反轉軌停止判斷，不使用舊 snapshot 補值。"
    if state == "REVERSAL_PRIORITY":
        return "前期流出後，Day-1 A-flow 翻正並在 30–90 分鐘持續增加，價格同步墊高且守在 VWAP 上方；升級反轉優先觀察。"
    if state == "REVERSAL_DAY1_EARLY":
        return "前期流出背景下已出現早盤價格翻強、A-flow 翻正與 VWAP 站上；先提早預警，等待 30–90 分鐘 Persistence 驗證。"
    if state == "PREVIOUS_FLOW_REVERSAL_DAY2_3_FAILURE":
        return "20D 仍屬前期流出，但近 5D 已先翻正；今天價格轉弱並跌回 VWAP 下，屬資金提前反轉後的 Day-2/3 延續失敗對照。"
    if state == "OUTFLOW_WATCH_NOT_TRIGGERED":
        return "前期流出條件成立，但今天價格沒有翻強、也沒有站回 VWAP；保留在 OUTFLOW WATCH，不升級 Day-1。"
    if state == "OUTFLOW_REVERSAL_WATCH":
        return "前期資金流出條件成立，但今日尚未同時出現價格反轉、A-flow 翻正與 VWAP 確認。"
    return "不符合前期流出型反轉軌，作為非反轉對照樣本。"
