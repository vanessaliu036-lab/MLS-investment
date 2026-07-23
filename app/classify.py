# -*- coding: utf-8 -*-
"""v4.2 標的分類：可操作／觀察／排除，群內再分象限。"""

from typing import Dict, List, Optional

from .intraday_filter import (
    NO_DATA, PASS, StockSnap, passes_filters, proxy_quadrant,
)

GROUP_ACTIONABLE = "可操作"
GROUP_WATCH = "觀察"
GROUP_EXCLUDE = "排除"
SUB_TRUE_ATTACK = "真攻擊追漲"
SUB_STRONG_ABSORB = "強惜售抄底"
SUB_EXTREME = "極端價失真"
SUB_FAKE_RED = "假紅衝高"
SUB_RESTING = "休息略過"
SUB_PENDING = "條件待確認"


def classify_one(s: StockSnap, regime: Optional[str] = None) -> dict:
    result = passes_filters(s, regime=regime)
    quadrant = proxy_quadrant(s.aflow, s.change_rate)
    has_nodata = any(state == NO_DATA for state in result["states"].values())

    if result["extreme"]:
        direction = "跌停" if s.change_rate < 0 else "漲停"
        return {"group": GROUP_EXCLUDE, "subgroup": SUB_EXTREME,
                "reason": f"逼近{direction}，aflow 失真、訊號不可信",
                "all_pass": False, "extreme": True}
    if quadrant == "休息":
        return {"group": GROUP_EXCLUDE, "subgroup": SUB_RESTING,
                "reason": "資金流出且下跌，無訊號",
                "all_pass": False, "extreme": False}
    if quadrant == "假紅":
        return {"group": GROUP_EXCLUDE, "subgroup": SUB_FAKE_RED,
                "reason": "漲但資金流出，假紅衝高勿追",
                "all_pass": False, "extreme": False}

    quadrant_pass = result["states"].get("regime_quadrant") == PASS
    intensity_pass = result["states"].get("aflow_intensity") == PASS
    aflow_pass = result["states"].get("aflow_positive") == PASS
    if quadrant == "真攻擊" and result["all_pass"]:
        return {"group": GROUP_ACTIONABLE, "subgroup": SUB_TRUE_ATTACK,
                "reason": "漲且買盤積極，追漲候選",
                "all_pass": True, "extreme": False}
    if quadrant == "惜售" and quadrant_pass and intensity_pass and aflow_pass:
        return {"group": GROUP_ACTIONABLE, "subgroup": SUB_STRONG_ABSORB,
                "reason": "跌深有大單承接，抄底候選（攻擊軌·配緊停損）",
                "all_pass": result["all_pass"], "extreme": False}

    return {"group": GROUP_WATCH, "subgroup": SUB_PENDING,
            "reason": "MA20 未接入待確認" if has_nodata else "部分條件未過，續觀察",
            "all_pass": False, "extreme": False}


def classify_all(snaps: List[StockSnap], regime: Optional[str] = None) -> dict:
    groups: Dict[str, Dict[str, list]] = {
        GROUP_ACTIONABLE: {}, GROUP_WATCH: {}, GROUP_EXCLUDE: {},
    }
    for snap in snaps:
        classified = classify_one(snap, regime=regime)
        row = {"code": snap.code, "change_rate": snap.change_rate,
               "aflow": snap.aflow,
               "quadrant": proxy_quadrant(snap.aflow, snap.change_rate),
               **classified}
        groups[classified["group"]].setdefault(classified["subgroup"], []).append(row)
    for items in groups[GROUP_ACTIONABLE].values():
        items.sort(key=lambda row: row["aflow"], reverse=True)
    counts = {group: sum(len(items) for items in subgroups.values())
              for group, subgroups in groups.items()}
    return {**groups, "counts": counts, "regime": regime}


GROUP_ORDER = [GROUP_ACTIONABLE, GROUP_WATCH, GROUP_EXCLUDE]
SUBGROUP_ORDER = {
    GROUP_ACTIONABLE: [SUB_TRUE_ATTACK, SUB_STRONG_ABSORB],
    GROUP_WATCH: [SUB_PENDING],
    GROUP_EXCLUDE: [SUB_FAKE_RED, SUB_EXTREME, SUB_RESTING],
}


def classify_flat(snaps: List[StockSnap], regime: Optional[str] = None) -> List[dict]:
    """攤平成可直接渲染的清單：可操作→觀察→排除，群內 aflow 強者優先。"""
    result = classify_all(snaps, regime=regime)
    flat: List[dict] = []
    for group in GROUP_ORDER:
        subgroups = result[group]
        order = SUBGROUP_ORDER.get(group, list(subgroups))
        for subgroup in order:
            rows = list(subgroups.get(subgroup, []))
            rows.sort(key=lambda row: row["aflow"], reverse=True)
            flat.extend(rows)
    return flat
