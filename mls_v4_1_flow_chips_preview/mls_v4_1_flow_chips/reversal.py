"""Research-only Extreme Outflow -> Day-1 Flow Reversal classifier.

This module is deliberately independent from the main MLS action engine.
It can identify an early reversal state, but it never grants trading authority.
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
) -> str:
    """Return the research state for the separate reversal warning track.

    The early state intentionally does not wait 30-90 minutes for persistence;
    that is the point of this bypass track. Persistence upgrades EARLY to
    PRIORITY rather than being required for the first warning.
    """
    if stale_price:
        return "STALE_PRICE_DATA"
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
    if state == "OUTFLOW_REVERSAL_WATCH":
        return "前期資金流出條件成立，但今日尚未同時出現價格反轉、A-flow 翻正與 VWAP 確認。"
    return "不符合前期流出型反轉軌，作為非反轉對照樣本。"
