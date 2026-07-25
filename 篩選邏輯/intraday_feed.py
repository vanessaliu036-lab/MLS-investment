"""
intraday_feed.py — 盤中即時行情 producer(唯一寫 quote_snap / aflow 的地方)

回答的問題:現在這一刻,價量與主動買賣差長怎樣?
資料來源:Shioaji 訂閱串流(價、量、內外盤)。盤中一次都不打 FinMind。

鐵律(對齊 phase.py):
  1. 只有 get_phase()==INTRADAY(交易日 09:00–13:30)才連線訂閱。
     週末/國定假日 get_phase()==CLOSED → 直接不啟動,Shioaji 一次都不連。
  2. 收盤(時段離開 INTRADAY)自動停止退出 —— 配合 systemd timer 09:00 拉起、自己收工。
  3. 只訂閱行情,絕不下單。憑證從環境變數讀,程式不持有明文。

主動買賣差(net_active):
  Shioaji tick.tick_type  1=外盤成交(主動買) / 2=內盤成交(主動賣) / 0=無法判定
  net_active = Σ主動買量 − Σ主動賣量。這是「推估」,盤後只當佐證(screen_post 權重最低)。

寫入(owner=intraday):
  quote_snap : code, price, change_rate, volume, bid_vol, ask_vol
  aflow      : code, active_buy, active_sell, net_active, method

日誌鐵律:Shioaji 行情列印量極大,務必走 journald,不要 append 到 /tmp
(/tmp tmpfs 會被塞爆 → 服務 crash loop,2026-07-24 已踩過)。
"""

from __future__ import annotations

import os
import sys
import time
import datetime as _dt
from pathlib import Path

import store
from phase import Phase, get_phase, today_tw

FLUSH_SEC = 15          # 每 15 秒把累積的主動買賣差落地一次
POLL_SEC = 2            # 主迴圈心跳
PLUGIN = "intraday"


def _universe() -> list[str]:
    ns: dict = {}
    exec((Path(__file__).parent / "config.py").read_text(encoding="utf-8"), ns)
    return list(ns["UNIVERSE"])


def _creds() -> tuple[str, str] | None:
    k = os.environ.get("SHIOAJI_API_KEY", "").strip()
    s = os.environ.get("SHIOAJI_SECRET_KEY", "").strip()
    return (k, s) if k and s else None


# ── 逐檔累積狀態(記憶體) ──────────────────────────────────────────
class _Acc:
    __slots__ = ("price", "ref", "volume", "buy", "sell", "bid_vol", "ask_vol")

    def __init__(self, ref: float = 0.0):
        self.price = 0.0
        self.ref = ref          # 參考價(昨收)→ 算漲跌幅
        self.volume = 0         # 當日累積成交張數
        self.buy = 0            # 主動買量(外盤)
        self.sell = 0           # 主動賣量(內盤)
        self.bid_vol = 0
        self.ask_vol = 0


STATE: dict[str, _Acc] = {}


def _on_tick(code: str, close: float, volume: int, tick_type: int, total_volume: int):
    a = STATE.get(code)
    if a is None:
        return
    a.price = close or a.price
    if total_volume:
        a.volume = total_volume
    if tick_type == 1:
        a.buy += volume or 0
    elif tick_type == 2:
        a.sell += volume or 0


def _on_bidask(code: str, bid_vol: int, ask_vol: int):
    a = STATE.get(code)
    if a is None:
        return
    a.bid_vol = bid_vol or 0
    a.ask_vol = ask_vol or 0


def _rows(d: _dt.date):
    dd = d.isoformat()
    now = _dt.datetime.now().isoformat(timespec="seconds")
    quotes, aflows = [], []
    for code, a in STATE.items():
        if a.price <= 0 and a.volume == 0:
            continue  # 這檔還沒有任何 tick,不寫空列
        chg = round((a.price - a.ref) / a.ref * 100, 2) if a.ref else 0.0
        quotes.append({
            "code": code, "data_date": dd, "price": a.price,
            "change_rate": chg, "volume": a.volume,
            "bid_vol": a.bid_vol, "ask_vol": a.ask_vol, "updated_at": now,
        })
        aflows.append({
            "code": code, "data_date": dd, "active_buy": a.buy,
            "active_sell": a.sell, "net_active": a.buy - a.sell,
            "method": "shioaji_tick", "updated_at": now,
        })
    return quotes, aflows


def _flush(d: _dt.date, db_path: str = "mls.db") -> int:
    quotes, aflows = _rows(d)
    if quotes:
        store.upsert_intraday("quote_snap", PLUGIN, quotes, db_path)
    if aflows:
        store.upsert_intraday("aflow", PLUGIN, aflows, db_path)
    return len(quotes)


# ── 主流程 ────────────────────────────────────────────────────────
def run(db_path: str = "mls.db") -> None:
    store.init_db(db_path)

    if get_phase() is not Phase.INTRADAY:
        print(f"[feed] 現在時段={get_phase().value},非盤中,不啟動 Shioaji。")
        return

    creds = _creds()
    if creds is None:
        print("[feed] 缺 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY,無法連線。"
              "盤中 quote/aflow 將維持 NO_DATA,名單照出(佐證缺一項而已)。")
        return

    try:
        import shioaji as sj
    except ImportError:
        print("[feed] 未安裝 shioaji(pip install shioaji)。跳過。")
        return

    codes = _universe()
    for c in codes:
        STATE[c] = _Acc()

    api = sj.Shioaji(simulation=False)   # 純訂閱行情;全程不呼叫任何下單 API
    api.login(api_key=creds[0], secret_key=creds[1])
    print(f"[feed] Shioaji 登入成功,訂閱 {len(codes)} 檔行情(僅行情,不下單)。")

    @api.on_tick_stk_v1()
    def _tick_cb(exchange, tick):
        try:
            _on_tick(tick.code, float(tick.close), int(tick.volume),
                     int(getattr(tick, "tick_type", 0) or 0),
                     int(getattr(tick, "total_volume", 0) or 0))
        except Exception:
            pass

    @api.on_bidask_stk_v1()
    def _bidask_cb(exchange, ba):
        try:
            bid = sum(int(x) for x in (ba.bid_volume or []))
            ask = sum(int(x) for x in (ba.ask_volume or []))
            _on_bidask(ba.code, bid, ask)
        except Exception:
            pass

    for c in codes:
        try:
            contract = api.Contracts.Stocks[c]
            if contract is None:
                continue
            STATE[c].ref = float(getattr(contract, "reference", 0) or 0)
            api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick,
                                version=sj.constant.QuoteVersion.v1)
            api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk,
                                version=sj.constant.QuoteVersion.v1)
        except Exception as e:
            print(f"[feed] 訂閱 {c} 失敗:{type(e).__name__}: {e}")

    d = today_tw()
    last_flush = 0.0
    try:
        while get_phase() is Phase.INTRADAY:
            time.sleep(POLL_SEC)
            if time.time() - last_flush >= FLUSH_SEC:
                n = _flush(d, db_path)
                last_flush = time.time()
                print(f"[feed] flush {n} 檔 quote/aflow @ {_dt.datetime.now():%H:%M:%S}")
    finally:
        _flush(d, db_path)
        try:
            api.logout()
        except Exception:
            pass
        print("[feed] 收盤,已停止訂閱並登出。")


def selftest(db_path: str = "mls.db") -> dict:
    """不連 Shioaji:灌合成 tick 驗證累積+落地路徑(給休市/CI 用)。"""
    store.init_db(db_path)
    STATE.clear()
    STATE["2330"] = _Acc(ref=2300.0)
    STATE["2317"] = _Acc(ref=100.0)
    _on_tick("2330", 2350.0, 3, tick_type=1, total_volume=1200)   # 主動買 3
    _on_tick("2330", 2351.0, 2, tick_type=2, total_volume=1202)   # 主動賣 2
    _on_bidask("2330", 500, 300)
    _on_tick("2317", 101.0, 5, tick_type=1, total_volume=800)
    d = today_tw()
    _flush(d, db_path)
    q = store.read_date("quote_snap", d, db_path)
    a = store.read_date("aflow", d, db_path)
    return {"quote_snap": q, "aflow": a}


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        r = selftest()
        import json
        print("selftest quote_snap:", json.dumps(r["quote_snap"], ensure_ascii=False))
        print("selftest aflow     :", json.dumps(r["aflow"], ensure_ascii=False))
    else:
        run()
