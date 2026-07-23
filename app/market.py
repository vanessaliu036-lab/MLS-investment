# -*- coding: utf-8 -*-
"""
market.py — 市場走向彙總公式（族群層，來自全池訂閱 aflow 加總）

全部為代理估值，會隨盤變動，收盤才定案 → UI 一律標「代理·未定案」。
"""

from typing import List, Dict
from .intraday_filter import proxy_quadrant


def sector_aflow(member_aflows: List[int]) -> int:
    """族群資金流向 = 該族群成分股 aflow 加總。"""
    return sum(int(a) for a in member_aflows)


def sector_heat(sectors: Dict[str, dict]) -> List[dict]:
    """
    族群熱圖。
    sectors: {族群名: {"aflows":[...], "change_rate":族群平均漲跌%}}
    回傳每族群 aflow 加總 + 代理象限，依 aflow 由大到小排序（熱圖排列）。
    """
    out = []
    for name, d in sectors.items():
        flow = sector_aflow(d["aflows"])
        out.append({
            "sector": name,
            "aflow": flow,
            "quadrant": proxy_quadrant(flow, d.get("change_rate", 0)),
            "proxy": True,   # 代理·收盤定案
        })
    out.sort(key=lambda x: x["aflow"], reverse=True)
    return out


def market_thermometer(heat: List[dict]) -> dict:
    """
    市場強度溫度計：in_up 族群數 vs out 族群數。
    強度 0–100：in 佔比線性映射。
        <40 防守 / 40–60 中性 / >60 進攻
    """
    if not heat:
        return {"score": 50, "verdict": "中性", "in_up": 0, "out": 0}
    in_up = sum(1 for h in heat if h["aflow"] > 0)
    out = sum(1 for h in heat if h["aflow"] <= 0)
    total = in_up + out
    score = round(in_up / total * 100) if total else 50
    verdict = "進攻" if score > 60 else ("防守" if score < 40 else "中性")
    return {"score": score, "verdict": verdict, "in_up": in_up, "out": out, "proxy": True}


def quadrant_distribution(stock_quadrants: List[str]) -> dict:
    """四象限即時分布計數（代理·未定案）。"""
    dist = {"真攻擊": 0, "惜售": 0, "假紅": 0, "休息": 0}
    for q in stock_quadrants:
        if q in dist:
            dist[q] += 1
    return dist
