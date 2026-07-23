# -*- coding: utf-8 -*-
"""
classify.py — 標的分類（三大群：可操作 / 觀察 / 排除，群內依象限細分）

主軸「可操作性」而非學術象限，因為盤中要的是「誰能看、誰先放、誰別碰」。
純用既有欄位（三態篩選 / 象限 / 極端價 / all_pass）判定，不需新資料。

三大群定義：
  可操作 ACTIONABLE：非極端價 + 象限對(真攻擊/強惜售) + 無 NO_DATA 且全 PASS。
                     群內子分「真攻擊追漲」/「強惜售抄底」（不同 playbook 不可混）。
  觀察   WATCH：有亮點但未全過（例如吸籌不足、或 MA20 未接入待確認），非極端價。
  排除   EXCLUDE：極端價失真（訊號不可信）、或象限休息/假紅（明確不符）。
"""

from typing import List, Dict, Optional
from .intraday_filter import (
    StockSnap, passes_filters, proxy_quadrant, is_extreme_price, PASS, NO_DATA,
)

GROUP_ACTIONABLE = "可操作"
GROUP_WATCH = "觀察"
GROUP_EXCLUDE = "排除"

# 子分類（象限層）
SUB_TRUE_ATTACK = "真攻擊追漲"
SUB_STRONG_ABSORB = "強惜售抄底"
SUB_EXTREME = "極端價失真"
SUB_FAKE_RED = "假紅衝高"
SUB_RESTING = "休息略過"
SUB_PENDING = "條件待確認"


def classify_one(s: StockSnap, regime: Optional[str] = None) -> dict:
    """
    單檔分類。回傳 {group, subgroup, reason, all_pass, extreme}。
    reason 為白話一句，供 UI 群組標題或 tooltip。
    """
    r = passes_filters(s, regime=regime)
    q = proxy_quadrant(s.aflow, s.change_rate)
    has_nodata = any(v == NO_DATA for v in r["states"].values())

    # 1) 極端價 → 一律排除（訊號不可信）
    if r["extreme"]:
        direction = "跌停" if s.change_rate < 0 else "漲停"
        return {"group": GROUP_EXCLUDE, "subgroup": SUB_EXTREME,
                "reason": f"逼近{direction}，aflow 失真、訊號不可信",
                "all_pass": False, "extreme": True}

    # 2) 象限休息/假紅 → 排除（明確不符）
    if q == "休息":
        return {"group": GROUP_EXCLUDE, "subgroup": SUB_RESTING,
                "reason": "資金流出且下跌，無訊號", "all_pass": False, "extreme": False}
    if q == "假紅":
        return {"group": GROUP_EXCLUDE, "subgroup": SUB_FAKE_RED,
                "reason": "漲但資金流出，假紅衝高勿追", "all_pass": False, "extreme": False}

    # 3) 可操作判定，依象限分追漲/抄底子群
    #    真攻擊追漲=引擎軌邏輯，需全過含 MA20；
    #    強惜售抄底=攻擊軌邏輯，不看 MA20（華邦電教訓：軌道不可混），
    #    只需非極端價 + 象限強惜售 + 吸籌強度足（即 regime_quadrant PASS）。
    q_pass = r["states"].get("regime_quadrant") == PASS
    intensity_pass = r["states"].get("aflow_intensity") == PASS
    aflow_pass = r["states"].get("aflow_positive") == PASS

    if q == "真攻擊":
        if r["all_pass"]:
            return {"group": GROUP_ACTIONABLE, "subgroup": SUB_TRUE_ATTACK,
                    "reason": "漲+買盤積極，追漲候選", "all_pass": True, "extreme": False}
    elif q == "惜售":
        # 強惜售抄底：攻擊軌，不綁 MA20
        if q_pass and intensity_pass and aflow_pass:
            return {"group": GROUP_ACTIONABLE, "subgroup": SUB_STRONG_ABSORB,
                    "reason": "跌深有大單承接，抄底候選（攻擊軌·配緊停損）",
                    "all_pass": r["all_pass"], "extreme": False}

    # 4) 其餘（象限對但條件沒齊，或引擎軌 MA20 未接入）→ 觀察
    reason = "MA20 未接入待確認" if has_nodata else "部分條件未過，續觀察"
    return {"group": GROUP_WATCH, "subgroup": SUB_PENDING,
            "reason": reason, "all_pass": False, "extreme": False}


def classify_all(snaps: List[StockSnap], regime: Optional[str] = None) -> dict:
    """
    全池分類，回傳巢狀結構供前端分組渲染：
        {
          "可操作": {"真攻擊追漲": [row...], "強惜售抄底": [row...]},
          "觀察":   {"條件待確認": [row...]},
          "排除":   {"極端價失真": [row...], "假紅衝高": [...], "休息略過": [...]},
          "counts": {"可操作": n, "觀察": n, "排除": n},
        }
    每 row 含 code / quadrant / 分類結果，可操作群內依吸籌強度排序。
    """
    groups: Dict[str, Dict[str, list]] = {
        GROUP_ACTIONABLE: {}, GROUP_WATCH: {}, GROUP_EXCLUDE: {},
    }
    for s in snaps:
        c = classify_one(s, regime=regime)
        row = {
            "code": s.code,
            "change_rate": s.change_rate,
            "aflow": s.aflow,
            "quadrant": proxy_quadrant(s.aflow, s.change_rate),
            **c,
        }
        groups[c["group"]].setdefault(c["subgroup"], []).append(row)

    # 可操作群內依 aflow 由大到小排（強者在前）
    for sub in groups[GROUP_ACTIONABLE].values():
        sub.sort(key=lambda r: r["aflow"], reverse=True)

    counts = {g: sum(len(v) for v in subs.values()) for g, subs in groups.items()}
    return {**groups, "counts": counts, "regime": regime}


# 群組顯示順序：可操作 → 觀察 → 排除（能進場的最前）
GROUP_ORDER = [GROUP_ACTIONABLE, GROUP_WATCH, GROUP_EXCLUDE]

# 子分類群內順序（同群聚在一起，子群也有固定序）
SUBGROUP_ORDER = {
    GROUP_ACTIONABLE: [SUB_TRUE_ATTACK, SUB_STRONG_ABSORB],
    GROUP_WATCH: [SUB_PENDING],
    GROUP_EXCLUDE: [SUB_FAKE_RED, SUB_EXTREME, SUB_RESTING],
}


def classify_flat(snaps: List[StockSnap], regime: Optional[str] = None) -> List[dict]:
    """
    攤平成「排序好的單一清單」，前端直接照順序印，整齊不交錯。
    排序規則：
        1. 群組：可操作 → 觀察 → 排除
        2. 子分類：依 SUBGROUP_ORDER
        3. 群內：aflow 由大到小（強者在前）
    每 row 標 group / subgroup，UI 可在群首插入分隔標題。
    """
    res = classify_all(snaps, regime=regime)
    flat: List[dict] = []
    for group in GROUP_ORDER:
        subs = res[group]
        for subgroup in SUBGROUP_ORDER.get(group, list(subs.keys())):
            rows = subs.get(subgroup, [])
            rows.sort(key=lambda r: r["aflow"], reverse=True)
            flat.extend(rows)
    return flat
