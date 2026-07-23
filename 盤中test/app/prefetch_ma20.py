# -*- coding: utf-8 -*-
"""
prefetch_ma20.py — 盤前計算 MA20 快取（本測試系統自帶，不依賴主站）

為什麼要這支：
    MA20（月線）是引擎軌命脈——「站回月線進場、跌破月線停損」。
    盤中訂閱只有即時價，沒有 MA20；缺了引擎軌整套癱瘓、篩選少一條。
    MA20 不是即時資料，是近 20 根日 K 收盤均價，盤前算一次快取即可。

鐵律：
    kbars 是查詢型 API，只能盤前抓、嚴禁盤中輪詢（撞「盤中禁輪詢」鐵律，超限回空值）。
    本模組只在開盤前（或每日排程 08:30 一次）執行。

用法：
    cache = build_ma20_cache(api, universe)      # 盤前跑一次
    ma20 = cache.get("2492")                     # 盤中即時價直接比這個
"""

import datetime as _dt
from typing import Dict, List, Optional


def compute_ma20(daily_closes: List[float], period: int = 20) -> Optional[float]:
    """
    純函式：由日收盤序列算 MA20，可單獨驗算。
    daily_closes: 由舊到新的日收盤價，至少 period 根，否則 None（不補造）。
    """
    if len(daily_closes) < period:
        return None
    window = daily_closes[-period:]
    return round(sum(window) / period, 2)


def _fetch_daily_closes(api, code: str, lookback_days: int = 40) -> List[float]:
    """
    盤前用 kbars 抓近 N 日日線收盤。lookback 給 40 確保含足 20 個交易日（扣假日）。
    僅盤前呼叫。回傳由舊到新的收盤序列。
    """
    end = _dt.date.today()
    start = end - _dt.timedelta(days=lookback_days)
    kbars = api.kbars(
        api.Contracts.Stocks[code],
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
    )
    # Shioaji kbars 為分 K，需彙總成日 K 收盤：取每日最後一筆 Close。
    import pandas as pd
    df = pd.DataFrame({"ts": kbars.ts, "Close": kbars.Close})
    df["date"] = pd.to_datetime(df["ts"]).dt.date
    daily = df.groupby("date")["Close"].last().tolist()
    return daily


def build_ma20_cache(api, universe: List[str],
                     period: int = 20) -> Dict[str, Optional[float]]:
    """
    盤前建立全池 MA20 快取。回傳 {code: ma20 or None}。
    None = 資料不足（新股等），盤中該檔 st_above_ma20 會判 NO_DATA(－)，不補造。
    失敗個股記 None 不中斷整批。
    """
    cache: Dict[str, Optional[float]] = {}
    for code in universe:
        try:
            closes = _fetch_daily_closes(api, code)
            cache[code] = compute_ma20(closes, period)
        except Exception:
            cache[code] = None
    return cache


def build_ma20_cache_from_closes(
        closes_map: Dict[str, List[float]], period: int = 20
) -> Dict[str, Optional[float]]:
    """
    注入口：若已有現成日收盤序列（例如另一資料源），直接算快取，不碰 kbars。
    測試與離線驗算用。
    """
    return {code: compute_ma20(cl, period) for code, cl in closes_map.items()}
