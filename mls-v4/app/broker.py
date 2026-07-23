"""
MLS v4.0 — broker.py
Shioaji（永豐）行情封裝。

穩定優先原則：
  · DATA_MODE=real 且金鑰齊全 → 連真實 Shioaji
  · 否則自動降級 demo（內建示意資料），系統照常啟動不崩。
真實接法已寫好，接上金鑰即用；未接時回傳 demo 快照。
"""
import random
import config as C

_api = None
_connected = False


def _demo_universe_snapshot():
    """demo 快照：用固定種子產生穩定的示意行情（每次啟動一致）。"""
    random.seed(20260717)
    snaps = []
    # 固定示意行情（對齊 chips demo，呈現有層次的合理盤面）
    # code: (base, chg, vr)
    demo = {
        "2327": (585, 1.8, 1.6), "2492": (132, 3.9, 2.0), "2456": (88, 0.7, 0.8),
        "2383": (412, 1.1, 1.4), "3037": (78, -1.8, 1.0), "3189": (145, 0.5, 0.9),
        "2303": (56, 0.4, 1.0), "5347": (98.5, 0.8, 1.2), "2330": (1045, -1.2, 0.85),
        "8150": (47.9, 1.7, 1.5), "2311": (142, 1.1, 1.3), "6147": (71.2, -2.4, 0.8),
        "2449": (98.7, 4.5, 2.4), "2337": (38.4, -0.6, 1.1), "2344": (28.6, 0.9, 1.0),
        "6488": (415, -0.8, 1.2), "5483": (238, 3.0, 1.3), "2454": (1360, -2.0, 1.22),
        "3034": (1080, 0.9, 1.1), "2345": (620, 3.5, 1.8), "4979": (315, 2.4, 1.5),
    }
    for code, (name, sector, typ) in C.UNIVERSE.items():
        base, chg, vr = demo.get(code, (100, 0.0, 1.0))
        close = round(base * (1 + chg / 100), 2)
        prev = base
        high = round(close * (1 + (0.008 if chg >= 0 else 0.002)), 2)
        low = round(close * (1 - (0.005 if chg >= 0 else 0.012)), 2)
        vol = int(8000 * vr)
        # 主動買賣差（估算）：漲多量大→流入
        af = int(vol * (chg / 100) * 1.0)
        snaps.append({
            "code": code, "name": name, "sector": sector, "track": typ,
            "close": close, "prev_close": prev, "high": high, "low": low,
            "change_rate": chg, "volume": vol, "volume_ratio": vr,
            "aflow": af,
        })
    return snaps


def connect():
    """連 Shioaji。失敗或未設金鑰→保持 demo 模式。"""
    global _api, _connected
    if C.DATA_MODE != "real" or not C.SHIOAJI_API_KEY:
        _connected = False
        return False
    try:
        import shioaji as sj
        _api = sj.Shioaji()
        _api.login(api_key=C.SHIOAJI_API_KEY,
                   secret_key=C.SHIOAJI_SECRET_KEY)
        if C.SHIOAJI_CA_PATH:
            _api.activate_ca(ca_path=C.SHIOAJI_CA_PATH,
                             ca_passwd=C.SHIOAJI_CA_PASSWD,
                             person_id=C.SHIOAJI_PERSON_ID)
        _connected = True
        return True
    except Exception as e:
        print(f"[broker] Shioaji 連線失敗，降級 demo：{e}")
        _connected = False
        return False


def is_connected():
    return _connected


def snapshot():
    """回傳觀察池全檔快照。real 模式取真實，否則 demo。"""
    if not _connected:
        return _demo_universe_snapshot()
    try:
        snaps = []
        for code, (name, sector, typ) in C.UNIVERSE.items():
            contract = _api.Contracts.Stocks[code]
            snap = _api.snapshots([contract])[0]
            chg = round((snap.close - snap.yesterday_close) /
                        snap.yesterday_close * 100, 2) if snap.yesterday_close else 0
            snaps.append({
                "code": code, "name": name, "sector": sector, "track": typ,
                "close": snap.close, "prev_close": snap.yesterday_close,
                "high": snap.high, "low": snap.low,
                "change_rate": chg, "volume": snap.total_volume,
                "volume_ratio": round(snap.total_volume /
                                      max(snap.average_volume or 1, 1), 2)
                                if hasattr(snap, "average_volume") else 1.0,
                "aflow": getattr(snap, "buy_volume", 0) - getattr(snap, "sell_volume", 0),
            })
        return snaps
    except Exception as e:
        print(f"[broker] snapshot 失敗，降級 demo：{e}")
        return _demo_universe_snapshot()


def ma20_of(code, snaps_close=None):
    """近20日收盤均線。real 模式取 K 線，demo 用收盤反推。"""
    if not _connected:
        # demo：以當前收盤反推一個合理 MA20
        cur = next((s["close"] for s in _demo_universe_snapshot()
                    if s["code"] == code), 100)
        return round(cur * 0.965, 2)
    try:
        from datetime import date, timedelta
        contract = _api.Contracts.Stocks[code]
        end = date.today()
        start = end - timedelta(days=40)
        kbars = _api.kbars(contract, start=str(start), end=str(end))
        import pandas as pd
        df = pd.DataFrame({**kbars})
        closes = df["Close"].tolist()[-20:]
        return round(sum(closes) / len(closes), 2) if closes else None
    except Exception as e:
        print(f"[broker] ma20 失敗：{e}")
        return None
