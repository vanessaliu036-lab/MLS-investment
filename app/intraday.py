# -*- coding: utf-8 -*-
"""
intraday.py — 盤中引擎骨架（訂閱模式，非輪詢）

⚠️ 鐵律：盤中即時資料一律用行情訂閱（api.subscribe），訂閱推播不計流量。
   嚴禁盤中輪詢 snapshots / ticks / kbars —— 超限會回空值，
   這正是「資金流一直抓不到」的最常見主因。

雙層訂閱：
  族群層：全池 50 檔訂 tick → callback 累加 aflow buffer（只要總量對）
  個股層：觀察池訂 tick + bidask → callback 餵狀態機 + 五檔判型態

抗斷線 / 抗崩潰：
  - callback 累加在記憶體 buffer（快）
  - 每輪 tick 把 buffer 快照落地 stock_state（耐崩，符合「無狀態依賴 DB」）
  - 心跳偵測：距上一筆 callback 超過閾值 → 判定斷線 → 重登重訂 + UI 標凍結

本檔為骨架，import 盤後既有模組（不重寫）：broker / decision / db。
"""

import time
from collections import defaultdict

from . import intraday_filter as F
from . import market as M

HEARTBEAT_TIMEOUT = 30

# bid = Shioaji bid_side_total_vol = 買盤成交總量
# ask = Shioaji ask_side_total_vol = 賣盤成交總量
_aflow_buffer = defaultdict(lambda: {"bid": 0, "ask": 0, "tickstream": []})
_last_tick_ts = {}
_bidask_buffer = {}


def on_tick(exchange, tick):
    """逐筆 callback：保存官方買／賣盤成交總量 + TickType 對照流。"""
    code = tick.code
    _aflow_buffer[code]["bid"] = int(tick.bid_side_total_vol)
    _aflow_buffer[code]["ask"] = int(tick.ask_side_total_vol)
    _aflow_buffer[code]["tickstream"].append((int(tick.tick_type), int(tick.volume)))
    _last_tick_ts[code] = time.time()


def on_bidask(exchange, bidask):
    _bidask_buffer[bidask.code] = {
        "bid_price": list(bidask.bid_price),
        "bid_volume": list(bidask.bid_volume),
        "ask_price": list(bidask.ask_price),
        "ask_volume": list(bidask.ask_volume),
    }
    _last_tick_ts[bidask.code] = time.time()


def subscribe_all(api, universe, watch_pool):
    api.quote.set_on_tick_stk_v1_callback(on_tick)
    api.quote.set_on_bidask_stk_v1_callback(on_bidask)

    for code in universe:
        api.quote.subscribe(api.Contracts.Stocks[code], quote_type="tick", version="v1")
    for code in watch_pool:
        api.quote.subscribe(api.Contracts.Stocks[code], quote_type="bidask", version="v1")


def heartbeat_ok(code, now=None):
    now = now or time.time()
    last = _last_tick_ts.get(code)
    if last is None:
        return False
    return (now - last) <= HEARTBEAT_TIMEOUT


def freshness_sec(code, now=None):
    now = now or time.time()
    last = _last_tick_ts.get(code)
    return None if last is None else round(now - last)


def reconnect(api, universe, watch_pool):
    try:
        api.logout()
    except Exception:
        pass
    subscribe_all(api, universe, watch_pool)


def build_snap(code, meta, prefetch):
    """組一檔 StockSnap；A-flow 一律走 canonical side-volume formula。"""
    buf = _aflow_buffer[code]
    aflow_a = F.aflow_from_sides(buf["bid"], buf["ask"])
    aflow_b = F.aflow_ticktype(buf["tickstream"])
    recon = F.aflow_reconcile(aflow_a, aflow_b)

    s = F.StockSnap(
        code=code,
        track=meta["track"],
        price=meta["price"],
        change_rate=meta["change_rate"],
        aflow=aflow_a,
        total_volume=meta.get("total_volume", 0),
        ma20=prefetch.get("ma20"),
        trigger_price=prefetch.get("trigger_price"),
        atr_stop=prefetch.get("atr_stop"),
        inst_buy_days=prefetch.get("inst_buy_days", 0),
    )
    return s, recon


def tick(api, universe, watch_pool, meta_of, prefetch_of, db=None):
    now = time.time()

    dead = [c for c in watch_pool if not heartbeat_ok(c, now)]
    if dead:
        reconnect(api, universe, watch_pool)

    sectors = _aggregate_sectors(universe, meta_of)
    heat = M.sector_heat(sectors)
    thermo = M.market_thermometer(heat)
    regime = F.market_regime(thermo["score"])

    events = []
    for code in watch_pool:
        s, recon = build_snap(code, meta_of(code), prefetch_of(code))
        filt = F.passes_filters(s, regime=regime)

        prev = _load_prev_state(code, db)
        new_state = F.next_state(prev, s)
        if new_state != prev:
            events.append({"code": code, "from": prev, "to": new_state, "ts": now})

        _persist_state(code, s, new_state, filt, recon, freshness_sec(code, now), db)

    quad_dist = M.quadrant_distribution(
        [F.proxy_quadrant(F.aflow_from_sides(_aflow_buffer[c]["bid"],
                                             _aflow_buffer[c]["ask"]),
                          meta_of(c)["change_rate"]) for c in universe]
    )

    return {"events": events, "heat": heat, "regime": regime,
            "thermometer": thermo, "quadrant_distribution": quad_dist}


def _load_prev_state(code, db):
    if db is None:
        return "觀察中"
    row = db.load_stock_state(code)
    return row["state"] if row else "觀察中"


def _persist_state(code, s, state, filt, recon, fresh, db):
    payload = {
        "code": code, "track": s.track, "state": state,
        "last_price": s.price, "change_rate": s.change_rate,
        "aflow": s.aflow, "aflow_diverged": recon["diverged"],
        "aflow_intensity": F.aflow_intensity(s.aflow, s.total_volume),
        "dist_ma20": F.dist_ma20(s.price, s.ma20),
        "quad": F.proxy_quadrant(s.aflow, s.change_rate),
        "pass_filters": filt["all_pass"],
        "filter_passed": filt["passed"],
        "filter_failed": filt["failed"],
        "filter_no_data": filt["no_data"],
        "filter_display": filt["display"],
        "filter_regime": filt.get("regime"),
        "extreme_price": filt.get("extreme", False),
        "fresh_sec": fresh,
        "data_stage": "intraday_est",
    }
    if db is not None:
        db.upsert_stock_state(payload)
    return payload


def _aggregate_sectors(universe, meta_of):
    return {}
