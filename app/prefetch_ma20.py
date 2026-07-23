# -*- coding: utf-8 -*-
"""盤前 MA20 快取；盤中只讀快取，絕不輪詢 kbars。"""

from typing import Dict, List, Optional


def compute_ma20(daily_closes: List[float], period: int = 20) -> Optional[float]:
    """至少有 period 根收盤才計算，資料不足回 None。"""
    if len(daily_closes) < period:
        return None
    return round(sum(daily_closes[-period:]) / period, 2)


def build_ma20_cache_from_closes(
    closes_map: Dict[str, List[float]], period: int = 20
) -> Dict[str, Optional[float]]:
    """離線/測試入口；不碰行情 API。"""
    return {code: compute_ma20(closes, period) for code, closes in closes_map.items()}


def build_ma20_cache(api, universe: List[str], period: int = 20):
    """盤前一次性抓日 K；失敗個股回 None，不中斷整批。"""
    import datetime as dt
    import pandas as pd

    end = dt.date.today()
    start = end - dt.timedelta(days=40)
    cache = {}
    for code in universe:
        try:
            kbars = api.kbars(
                api.Contracts.Stocks[code],
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
            )
            df = pd.DataFrame({"ts": kbars.ts, "Close": kbars.Close})
            df["date"] = pd.to_datetime(df["ts"]).dt.date
            closes = df.groupby("date")["Close"].last().tolist()
            cache[code] = compute_ma20(closes, period)
        except Exception:
            cache[code] = None
    return cache
