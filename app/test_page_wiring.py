# -*- coding: utf-8 -*-
"""測試頁的 MA20 快取與每檔本地解讀接線工具。"""

from typing import Callable, Dict, List, Optional

from . import ai_explain
from . import intraday_filter as F


def make_prefetch_of(
    ma20_cache: Dict[str, Optional[float]],
    yesterday_vol: Dict[str, int],
    atr_map: Dict[str, float],
    trigger_map: Dict[str, float],
    inst_buy_days: Optional[Dict[str, int]] = None,
) -> Callable[[str], dict]:
    inst_buy_days = inst_buy_days or {}

    def prefetch_of(code: str) -> dict:
        return {
            "ma20": ma20_cache.get(code),
            "yesterday_volume": yesterday_vol.get(code, 0),
            "atr_stop": atr_map.get(code),
            "trigger_price": trigger_map.get(code),
            "inst_buy_days": inst_buy_days.get(code, 0),
        }
    return prefetch_of


def build_rows(
    codes: List[str],
    meta_of: Callable[[str], dict],
    prefetch_of: Callable[[str], dict],
    regime: str,
    claude_api_key: Optional[str] = None,
) -> List[dict]:
    rows = []
    for code in codes:
        meta = meta_of(code)
        prefetch = prefetch_of(code)
        snap = F.StockSnap(
            code=code, track=meta.get("track", "attack"),
            price=meta["price"], change_rate=meta["change_rate"],
            aflow=meta["aflow"], total_volume=meta.get("total_volume", 0),
            ma20=prefetch.get("ma20"),
            trigger_price=prefetch.get("trigger_price"),
            atr_stop=prefetch.get("atr_stop"),
            inst_buy_days=prefetch.get("inst_buy_days", 0),
        )
        filters = F.passes_filters(snap, regime=regime)
        explanation = (ai_explain.claude_explain(snap, regime, claude_api_key)
                       if claude_api_key else ai_explain.local_explain(snap, regime))
        rows.append({
            "code": code, "name": meta.get("name", ""),
            "price": snap.price, "change_rate": snap.change_rate,
            "aflow": snap.aflow, "quadrant": F.proxy_quadrant(snap.aflow, snap.change_rate),
            "total_volume": snap.total_volume, "ma20": snap.ma20,
            "filter_display": filters["display"],
            "filter_passed": filters["passed"],
            "filter_failed": filters["failed"],
            "filter_no_data": filters["no_data"],
            "extreme_price": filters["extreme"],
            "all_pass": filters["all_pass"], "ai": explanation,
        })
    return rows
