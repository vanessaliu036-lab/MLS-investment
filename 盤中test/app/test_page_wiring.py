# -*- coding: utf-8 -*-
"""
test_page_wiring.py — /intraday-test 頁完整接線（MA20 快取 + AI 解讀欄）

這支把三塊接起來，讓測試頁那欄「—站上MA20」活過來、並每檔多一行 AI 白話：
  1. 盤前跑一次 build_ma20_cache → MA20 快取
  2. prefetch_of(code) 把 MA20 餵進 build_snap
  3. 每檔跑 local_explain 生一行 AI 解讀，塞進回傳給前端

在 VPS 上用法：
    from app.test_page_wiring import make_prefetch_of, build_rows
    ma20_cache = prefetch_ma20.build_ma20_cache(api, UNIVERSE)   # 盤前 08:30 一次
    prefetch_of = make_prefetch_of(ma20_cache, yesterday_vol, atr_map, trigger_map)
    rows = build_rows(UNIVERSE, meta_of, prefetch_of, regime)    # 每輪 tick 呼叫

前端新增一欄 row["ai"] 直接印即可。
"""

from typing import Callable, Dict, List, Optional
from . import intraday_filter as F
from . import ai_explain


def make_prefetch_of(
    ma20_cache: Dict[str, Optional[float]],
    yesterday_vol: Dict[str, int],
    atr_map: Dict[str, float],
    trigger_map: Dict[str, float],
    inst_buy_days: Optional[Dict[str, int]] = None,
    inst_chips: Optional[Dict[str, dict]] = None,
) -> Callable[[str], dict]:
    """
    組 prefetch_of：把盤前快取包成 build_snap 需要的 dict。
    MA20 未接入的個股 → cache 給 None → 該檔 st_above_ma20 判 NO_DATA（顯示「—」）。

    inst_chips 格式（2026-07-20 新增）：
        {"2330": {"foreign": 500, "trust": 100, "dealer": -50, "total": 550,
                  "prev_total": 200}, ...}
    沒餵或缺欄位 → 對應欄位 None，走資料缺失降級。
    """
    inst_buy_days = inst_buy_days or {}
    inst_chips = inst_chips or {}

    def prefetch_of(code: str) -> dict:
        chip = inst_chips.get(code, {}) if inst_chips else {}
        return {
            "ma20": ma20_cache.get(code),               # None → NO_DATA，不補造
            "yesterday_volume": yesterday_vol.get(code, 0),
            "atr_stop": atr_map.get(code),
            "trigger_price": trigger_map.get(code),
            "inst_buy_days": inst_buy_days.get(code, 0),
            # 法人即時（缺 → None）
            "inst_foreign": chip.get("foreign"),
            "inst_trust": chip.get("trust"),
            "inst_dealer": chip.get("dealer"),
            "inst_net_total": chip.get("total"),
        }
    return prefetch_of


def build_rows(
    codes: List[str],
    meta_of: Callable[[str], dict],
    prefetch_of: Callable[[str], dict],
    regime: str,
    claude_api_key: Optional[str] = None,
    flow_of: Optional[Callable[[str], Optional[dict]]] = None,
) -> List[dict]:
    """
    產出前端表格每列資料，含三態篩選 + AI 白話解讀欄。
    claude_api_key 有給就走 Claude 潤飾，沒給就純本地解讀（不開天窗）。
    flow_of 可接入盤中訂閱 buffer（例如 ``intraday.flow_snapshot``）。
    若未提供 flow_of，才向 meta_of 取既有的 aflow；避免把缺資料誤當成 0。
    """
    rows = []
    for code in codes:
        meta = meta_of(code)
        pf = prefetch_of(code)
        flow = flow_of(code) if flow_of else None
        if flow is None and "aflow" not in meta:
            raise ValueError(
                f"{code} 尚未接入盤中資金流向：請等待第一筆 tick，"
                "或將 intraday.flow_snapshot 傳入 flow_of"
            )
        aflow = flow["aflow"] if flow is not None else meta["aflow"]
        total_volume = (flow.get("total_volume", 0) if flow is not None
                        else meta.get("total_volume", 0))
        s = F.StockSnap(
            code=code,
            track=meta.get("track", "attack"),
            price=meta["price"],
            change_rate=meta["change_rate"],
            aflow=aflow,
            total_volume=total_volume,
            ma20=pf.get("ma20"),
            trigger_price=pf.get("trigger_price"),
            atr_stop=pf.get("atr_stop"),
            inst_buy_days=pf.get("inst_buy_days", 0),
            inst_foreign=pf.get("inst_foreign"),
            inst_trust=pf.get("inst_trust"),
            inst_dealer=pf.get("inst_dealer"),
            inst_net_total=pf.get("inst_net_total"),
        )
        filt = F.passes_filters(s, regime=regime)
        ai = (ai_explain.claude_explain(s, regime, claude_api_key)
              if claude_api_key else ai_explain.local_explain(s, regime))

        rows.append({
            "code": code,
            "name": meta.get("name", ""),
            "price": s.price,
            "change_rate": s.change_rate,
            "aflow": s.aflow,
            "quadrant": F.proxy_quadrant(s.aflow, s.change_rate),
            "total_volume": s.total_volume,
            "ma20": s.ma20,                          # None → 前端顯示「—」
            "filter_display": filt["display"],       # ✓/✗/— 三態
            "filter_passed": filt["passed"],
            "filter_failed": filt["failed"],
            "filter_no_data": filt["no_data"],
            "extreme_price": filt["extreme"],
            "all_pass": filt["all_pass"],
            "ai": ai,                                # ← 每檔一行 AI 白話解讀
            # 法人校驗（2026-07-20 新增）
            "inst_net_total": filt.get("inst_net_total"),
            "inst_warnings": filt.get("inst_validation", {}).get("warnings", []),
            "inst_hard_block": filt.get("inst_validation", {}).get("hard_block", False),
            "inst_sell_blocks_absorb": filt.get("inst_sell_blocks_absorb", False),
        })
    return rows
