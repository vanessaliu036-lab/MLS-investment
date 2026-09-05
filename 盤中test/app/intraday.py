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

# 盤後既有模組（直接複用，不重寫）
# import broker, decision, db
from . import intraday_filter as F
from . import market as M

HEARTBEAT_TIMEOUT = 30   # 秒；開盤時段超過此值無 tick → 判定斷線

# ---- 記憶體 buffer（callback 累加，每輪落地）----
_aflow_buffer = defaultdict(lambda: {"bid": 0, "ask": 0, "tickstream": []})
_last_tick_ts = {}       # code -> 最後一筆 callback 時間戳
_bidask_buffer = {}      # code -> 最新五檔


def flow_ready(code):
    """是否已收到該檔第一筆盤中成交推播。

    aflow=0 可能是合法結果，因此不能用 aflow 是否為零判斷資料缺失；
    必須用 callback 寫入的 timestamp 判斷。
    """
    return code in _last_tick_ts and code in _aflow_buffer


def flow_status(codes):
    """回傳前端可直接顯示的盤中資金流向就緒狀態。"""
    codes = list(codes)
    ready_codes = [code for code in codes if flow_ready(code)]
    return {
        "ready": bool(ready_codes),
        "ready_count": len(ready_codes),
        "total_count": len(codes),
        "message": "盤中資金流向已接收" if ready_codes else "等待盤中資金流向",
    }


def flow_snapshot(code):
    """取得一檔已接收的盤中資金流向，未就緒時回傳 None。"""
    if not flow_ready(code):
        return None
    buf = _aflow_buffer[code]
    return {
        "aflow": F.aflow_from_sides(buf["bid"], buf["ask"]),
        "total_volume": sum(volume for _, volume in buf["tickstream"]),
        "fresh_sec": freshness_sec(code),
    }


# =====================================================================
# 訂閱 callback
# =====================================================================

def on_tick(exchange, tick):
    """逐筆 callback：累加官方總量 + 記逐筆供 TickType 對照算法。"""
    code = tick.code
    # 使用 getattr 讓不同行情 SDK 版本缺少欄位時明確保持未就緒，
    # 不把 None 轉成 0 後誤判成「沒有資金流」。
    bid = getattr(tick, "bid_side_total_vol", None)
    ask = getattr(tick, "ask_side_total_vol", None)
    tick_type = getattr(tick, "tick_type", None)
    volume = getattr(tick, "volume", None)
    if bid is None or ask is None or tick_type is None or volume is None:
        return
    _aflow_buffer[code]["bid"] = int(bid)
    _aflow_buffer[code]["ask"] = int(ask)
    _aflow_buffer[code]["tickstream"].append((int(tick_type), int(volume)))
    _last_tick_ts[code] = time.time()


def on_bidask(exchange, bidask):
    """五檔 callback：只有觀察池個股訂閱。"""
    _bidask_buffer[bidask.code] = {
        "bid_price": list(bidask.bid_price),
        "bid_volume": list(bidask.bid_volume),
        "ask_price": list(bidask.ask_price),
        "ask_volume": list(bidask.ask_volume),
    }
    _last_tick_ts[bidask.code] = time.time()


# =====================================================================
# 訂閱管理
# =====================================================================

def subscribe_all(api, universe, watch_pool):
    """
    雙層訂閱。訂閱池固定不動（避免頻繁 sub/unsub 觸發防濫用）。
    universe   : 全池 50 檔（族群層，只訂 tick）
    watch_pool : 觀察池（個股層，訂 tick + bidask）
    """
    api.quote.set_on_tick_stk_v1_callback(on_tick)
    api.quote.set_on_bidask_stk_v1_callback(on_bidask)

    for code in universe:
        api.quote.subscribe(api.Contracts.Stocks[code],
                            quote_type="tick", version="v1")
    for code in watch_pool:
        api.quote.subscribe(api.Contracts.Stocks[code],
                            quote_type="bidask", version="v1")


def heartbeat_ok(code, now=None):
    """心跳偵測：該檔逐筆是否新鮮。斷線 buffer 會靜默凍結，必須主動偵測。"""
    now = now or time.time()
    last = _last_tick_ts.get(code)
    if last is None:
        return False
    return (now - last) <= HEARTBEAT_TIMEOUT


def freshness_sec(code, now=None):
    """回傳該檔資料幾秒前更新（UI 逐檔標記用）。"""
    now = now or time.time()
    last = _last_tick_ts.get(code)
    return None if last is None else round(now - last)


def reconnect(api, universe, watch_pool):
    """斷線重登重訂。累積量已落地 stock_state，重訂後從 DB 基準續算。"""
    try:
        api.logout()
    except Exception:
        pass
    # api.login(...)  # 帶回原 api_key/secret_key
    subscribe_all(api, universe, watch_pool)


# =====================================================================
# 每輪 tick（APScheduler interval 3–5 秒；09:00–13:30，收盤停）
# =====================================================================

def build_snap(code, meta, prefetch):
    """
    組一檔 StockSnap。
    meta     : {"track","change_rate","price"} 來自 tick buffer / 最新成交
    prefetch : {"ma20","trigger_price","atr_stop","yesterday_volume","inst_buy_days"}
               盤前算好快取 + 昨日盤後底本
    """
    buf = _aflow_buffer[code]
    aflow_a = F.aflow_from_sides(buf["bid"], buf["ask"])
    aflow_b = F.aflow_ticktype(buf["tickstream"])
    recon = F.aflow_reconcile(aflow_a, aflow_b)   # 背離 → UI 標校驗異常

    s = F.StockSnap(
        code=code,
        track=meta["track"],
        price=meta["price"],
        change_rate=meta["change_rate"],
        aflow=aflow_a,                    # 主用官方算法
        total_volume=meta.get("total_volume", 0),   # 算 aflow 強度佔比
        ma20=prefetch.get("ma20"),
        trigger_price=prefetch.get("trigger_price"),
        atr_stop=prefetch.get("atr_stop"),
        inst_buy_days=prefetch.get("inst_buy_days", 0),
        # 法人即時籌碼（2026-07-20 新增；沒抓到 → None 走資料缺失降級）
        inst_foreign=prefetch.get("inst_foreign"),
        inst_trust=prefetch.get("inst_trust"),
        inst_dealer=prefetch.get("inst_dealer"),
        inst_net_total=prefetch.get("inst_net_total"),
    )
    return s, recon


def tick(api, universe, watch_pool, meta_of, prefetch_of, db=None):
    """
    每 N 秒呼叫一次。
    meta_of(code)     -> {"track","price","change_rate"}
    prefetch_of(code) -> 盤前快取 + 昨日底本 dict
    """
    now = time.time()
    flow = flow_status(universe)

    # 1) 心跳偵測 → 斷了就重連
    dead = [c for c in watch_pool if not heartbeat_ok(c, now)]
    if dead:
        # UI 會依 freshness_sec 標「凍結於 HH:MM」
        reconnect(api, universe, watch_pool)

    # 訂閱剛啟動或斷線重連後，不能用空 buffer 產生假訊號。
    # 呼叫端可用 status 顯示「等待盤中資金流向」，下一輪再重試。
    if not flow["ready"]:
        return {
            "events": [], "heat": [], "regime": None,
            "thermometer": None, "quadrant_distribution": {},
            "status": "waiting_for_intraday_flow", "flow": flow,
        }

    # 2) 先算族群層 → 得溫度計 → 判盤勢（象限篩選要依盤勢動態切換）
    sectors = _aggregate_sectors(universe, meta_of)      # {族群:{aflows,change_rate}}
    heat = M.sector_heat(sectors)
    thermo = M.market_thermometer(heat)
    regime = F.market_regime(thermo["score"])            # 攻擊盤/防守盤/震盪盤

    # 3) 個股層：跑狀態機 + 依盤勢篩選 + 落地
    events = []
    for code in watch_pool:
        s, recon = build_snap(code, meta_of(code), prefetch_of(code))
        filt = F.passes_filters(s, regime=regime)        # 象限條件依盤勢自動切換

        prev = _load_prev_state(code, db)                # 從 stock_state 讀上一狀態
        new_state = F.next_state(prev, s)
        if new_state != prev:                            # 狀態轉移才發事件
            events.append({"code": code, "from": prev, "to": new_state, "ts": now})

        _persist_state(code, s, new_state, filt, recon, freshness_sec(code, now), db)

    # 4) 象限分布
    quad_dist = M.quadrant_distribution(
        [F.proxy_quadrant(F.aflow_from_sides(_aflow_buffer[c]["bid"],
                                             _aflow_buffer[c]["ask"]),
                          meta_of(c)["change_rate"]) for c in universe]
    )

    return {"events": events, "heat": heat, "regime": regime,
            "thermometer": thermo, "quadrant_distribution": quad_dist,
            "status": "ready", "flow": flow}


# =====================================================================
# 落地 / 讀取（接盤後 db.py 的 stock_state 表）
# =====================================================================

def _load_prev_state(code, db):
    if db is None:
        return "觀察中"
    row = db.load_stock_state(code)      # 盤後 db.py 提供
    return row["state"] if row else "觀察中"


def _persist_state(code, s, state, filt, recon, fresh, db):
    """buffer 快照落地 stock_state（耐崩，重啟從此重播不歸零）。"""
    payload = {
        "code": code, "track": s.track, "state": state,
        "last_price": s.price, "change_rate": s.change_rate,
        "aflow": s.aflow, "aflow_diverged": recon["diverged"],
        "aflow_intensity": F.aflow_intensity(s.aflow, s.total_volume),
        "dist_ma20": F.dist_ma20(s.price, s.ma20),
        "quad": F.proxy_quadrant(s.aflow, s.change_rate),
        "pass_filters": filt["all_pass"],
        "filter_passed": filt["passed"],     # 前端直接印（綠 ✓）
        "filter_failed": filt["failed"],     # 前端直接印（灰 ✗）
        "filter_no_data": filt["no_data"],   # 前端直接印（－，未接入/極端價失真）
        "filter_display": filt["display"],   # 一行貼上，含 ✓/✗/－ 三態
        "extreme_price": filt["extreme"],    # 跌停/漲停 → UI 標「極端價·訊號不可信」
        "fresh_sec": fresh,
        "data_stage": "intraday_est",
    }
    if db is not None:
        db.upsert_stock_state(payload)
    return payload


def _aggregate_sectors(universe, meta_of):
    """用已收到的盤中 aflow 建立族群資金流向。

    meta_of 可提供 ``sector`` 或 ``industry``；若尚未接入族群表，
    先歸入「市場總體」，至少讓市場溫度計有真實盤中資金流向可用。
    """
    sectors = defaultdict(lambda: {"aflows": [], "changes": []})
    for code in universe:
        if not flow_ready(code):
            continue
        meta = meta_of(code) or {}
        name = meta.get("sector") or meta.get("industry") or "市場總體"
        buf = _aflow_buffer[code]
        sectors[name]["aflows"].append(F.aflow_from_sides(buf["bid"], buf["ask"]))
        if meta.get("change_rate") is not None:
            sectors[name]["changes"].append(float(meta["change_rate"]))
    return {
        name: {
            "aflows": data["aflows"],
            "change_rate": (sum(data["changes"]) / len(data["changes"]))
            if data["changes"] else 0,
        }
        for name, data in sectors.items()
    }
