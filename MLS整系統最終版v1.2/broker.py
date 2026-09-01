"""
MLS 標準版 — broker.py
永豐 Shioaji 連線層。即時數據唯一來源。
金鑰一律從環境變數讀取,程式碼內不含任何金鑰:
    SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY
只用行情功能,不下單、不啟用 CA 憑證。
"""

import os
import time
from datetime import datetime, timezone, timedelta
import shioaji as sj

_api = None
_last_login = 0
RELOGIN_SEC = 20 * 3600   # Shioaji 需每24h重登,提前於20h重連
TW_TZ = timezone(timedelta(hours=8))


def _aggregate_tick_flow(ticks):
    """彙總當日逐筆成交內外盤。

    Shioaji Ticks.tick_type: 1=外盤(主動買),2=內盤(主動賣),0=無法判定。
    無法判定的成交不納入 classified_volume，也不硬猜方向。
    """
    buy = sell = 0
    volumes = getattr(ticks, "volume", None) or []
    sides = getattr(ticks, "tick_type", None) or []
    for volume, side in zip(volumes, sides):
        try:
            v = int(volume or 0)
        except (TypeError, ValueError):
            continue
        sval = str(getattr(side, "value", side))
        if sval in ("1", "Buy", "TickType.Buy"):
            buy += v
        elif sval in ("2", "Sell", "TickType.Sell"):
            sell += v
    return {
        "active_buy_volume": buy,
        "active_sell_volume": sell,
        "active_diff": buy - sell,
        "classified_volume": buy + sell,
    }


def active_flow_today(code, date=None):
    """個股卡用：由真實逐筆成交計算全日主動買／賣與買賣差。

    不使用 Snapshot.buy_volume / sell_volume，因官方定義是最佳委買／委賣掛單量，
    並不是累積的主動買／賣成交量。此查詢只供單一個股按需取得，避免全市場輪詢。
    """
    api = get_api()
    trade_date = date or datetime.now(TW_TZ).strftime("%Y-%m-%d")
    contract = api.Contracts.Stocks[code]
    ticks = api.ticks(contract=contract, date=trade_date)
    out = _aggregate_tick_flow(ticks)
    out.update({"date": trade_date, "source": "shioaji_ticks"})
    return out


def get_api():
    """取得已登入的 Shioaji 實例;超過20小時自動重登。"""
    global _api, _last_login
    if _api is not None and (time.time() - _last_login) < RELOGIN_SEC:
        return _api
    if _api is not None:
        try:
            _api.logout()
        except Exception:
            pass
    _api = sj.Shioaji(simulation=False)   # 測試時可改 True
    _api.login(
        api_key=os.environ["SHIOAJI_API_KEY"],
        secret_key=os.environ["SHIOAJI_SECRET_KEY"],
        fetch_contract=True,
    )
    _last_login = time.time()
    print("[broker] Shioaji 登入成功")
    return _api


def market_scan_codes():
    """
    三排行榜聯集 → 全市場活躍股代碼(不佔訂閱額度)。
    """
    from config import SCANNER_TOP_N
    api = get_api()
    codes = set()
    for st in (
        sj.constant.ScannerType.ChangePercentRank,
        sj.constant.ScannerType.AmountRank,
        sj.constant.ScannerType.VolumeRank,
    ):
        try:
            for r in api.scanners(scanner_type=st, count=SCANNER_TOP_N):
                codes.add(r.code)
        except Exception as e:
            print(f"[broker] scanner {st} 失敗: {e}")
    return list(codes)


def batch_snapshots(codes):
    """
    批次快照(不佔訂閱額度)。回傳 list[dict] 統一欄位:
    code, price, open, high, low, change_rate(%), volume_ratio,
    total_volume, total_amount, avg_price, tick_type。

    單一個股查詢時，既有 buy_volume / sell_volume 欄位改接當日 ticks 的
    真實外盤／內盤成交量，另用 bid_volume / ask_volume 保留快照掛單量。
    多檔掃描維持既有行為，避免本修正改動凍結中的主引擎計算。
    """
    api = get_api()
    single_flow = None
    if len(codes) == 1:
        try:
            single_flow = active_flow_today(codes[0])
        except Exception as e:
            print(f"[broker] active flow {codes[0]} 失敗: {e}")

    contracts = []
    for c in codes:
        try:
            contracts.append(api.Contracts.Stocks[c])
        except Exception:
            continue

    out = []
    for i in range(0, len(contracts), 400):      # snapshots 分批,控節奏防超限
        try:
            snaps = api.snapshots(contracts[i:i + 400])
        except Exception as e:
            print(f"[broker] snapshots 批次失敗: {e}")
            time.sleep(1)
            continue
        for s in snaps:
            quote_buy = getattr(s, "buy_volume", 0) or 0
            quote_sell = getattr(s, "sell_volume", 0) or 0
            is_single = len(codes) == 1
            out.append({
                "code": s.code,
                "price": s.close,
                "open": s.open, "high": s.high, "low": s.low,
                "change_rate": s.change_rate,
                "volume_ratio": getattr(s, "volume_ratio", 0) or 0,
                "total_volume": (s.total_volume or 0),
                "total_amount": (s.total_amount or 0),
                "avg_price": getattr(s, "average_price", None),
                "tick_type": getattr(s, "tick_type", None),
                "bid_volume": quote_buy,
                "ask_volume": quote_sell,
                "buy_volume": ((single_flow or {}).get("active_buy_volume")
                               if is_single else quote_buy),
                "sell_volume": ((single_flow or {}).get("active_sell_volume")
                                if is_single else quote_sell),
                "active_flow_diff": ((single_flow or {}).get("active_diff")
                                     if is_single else None),
                "active_flow_source": ((single_flow or {}).get("source")
                                       if is_single else None),
            })
        time.sleep(0.3)
    return out


def index_snapshot():
    """加權指數快照(Shioaji 指數合約 TSE001)。"""
    api = get_api()
    try:
        s = api.snapshots([api.Contracts.Indexs.TSE["001"]])[0]
        return {
            "index": s.close,
            "index_pct": round(s.change_rate, 2),
            "amount_100m": round((s.total_amount or 0) / 1e8, 0),
        }
    except Exception as e:
        print(f"[broker] 指數快照失敗: {e}")
        return {}


def daily_kbars(code, days=70):
    """
    日K(供 MA20 / 60日前高 計算)。回傳 list[dict(date, close, high)]。
    """
    import pandas as pd
    from datetime import datetime, timedelta
    api = get_api()
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    try:
        kb = api.kbars(api.Contracts.Stocks[code], start=start, end=end)
        df = pd.DataFrame({**kb})
        df["ts"] = pd.to_datetime(df["ts"])
        g = df.groupby(df["ts"].dt.date)
        daily = pd.DataFrame({
            "close": g["Close"].last(),
            "high": g["High"].max(),
            "low": g["Low"].min(),      # v2.4.1:補真實低點(六點50%回測/KD/ATR 轉精確)
            "open": g["Open"].first(),
            "volume": g["Volume"].sum(),
        }).tail(days)
        return daily.reset_index().to_dict("records")
    except Exception as e:
        print(f"[broker] kbars {code} 失敗: {e}")
        return []
