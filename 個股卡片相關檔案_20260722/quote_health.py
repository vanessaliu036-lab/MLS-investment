# -*- coding: utf-8 -*-
"""quote_health.py — 盤中報價健康度判定 + MIS 備援疊加（行程內，不另開登入）

定位（依 Vanessa 規劃 §1–§3 §6，行程內輕量版）：
    這支「不」登入 Shioaji、「不」另開行程——那正是互踢 session 的來源。它只吃
    broker 現成的 buffer 列，判斷行情層是死是活；活 → 原封輸出並標 LIVE；死 →
    抓 TWSE MIS 疊上價/量、把 aflow 標 UNAVAILABLE(絕不用五檔偽裝)。

    Shioaji 掛掉 → 只損失 aflow，價量/量比/突破照跑；不再拖垮整個盤中畫面。

判定門檻（全域最後一筆 tick 進展；沿用 gateway 常數口徑）：
    tick 全域無進展 >20s 或 buffer 空 → 判定行情層死 → 價量走 MIS(BACKUP)
    tick >15s 未更新                  → aflow_status=UNAVAILABLE(價量若仍新可暫留 shioaji)
    以上皆否                          → LIVE

    每列掛上：_price_source / _quote_status / _aflow_unavailable / _last_tick_age，
    由 vps_intraday_test._row 讀出後填進對外欄位。
"""
from __future__ import annotations

import time
import datetime as _dt

import mis_source

TICK_DEAD = 20        # 全域無 tick 進展 >20s → 行情層死，價量切 MIS
AFLOW_UNAVAIL = 15    # aflow >15s 未更新 → UNAVAILABLE
MIS_POLL_SEC = 5      # 備援輪詢節流

_TW = _dt.timezone(_dt.timedelta(hours=8))

# 全域健康狀態（模組級；intraday_test 每幾秒呼叫一次）
_S = {"last_tick_ts": 0.0, "last_vol_sum": -1, "last_mis_ts": 0.0, "mis": {}}


def _market_hours():
    n = _dt.datetime.now(_TW)
    if n.weekday() >= 5:
        return False
    m = n.hour * 60 + n.minute
    return 9 * 60 <= m <= 13 * 60 + 35


def _universe():
    try:
        import config
        return list(getattr(config, "UNIVERSE", []))
    except Exception:
        return []


def _poll_mis(codes):
    now = time.time()
    if now - _S["last_mis_ts"] > MIS_POLL_SEC or not _S["mis"]:
        try:
            _S["mis"] = mis_source.fetch(codes)
            _S["last_mis_ts"] = now
        except Exception as e:
            print(f"[quote_health] mis fail: {type(e).__name__}: {e}", flush=True)
    return _S["mis"]


def apply(rows: list[dict]):
    """輸入 broker.raw_buffer_snapshots() 的列；回傳 (rows_with_flags, meta)。

    rows 會被原地補上 _price_source/_quote_status/_aflow_unavailable/_last_tick_age；
    行情層死時，rows 的 price/change_rate/total_volume 會被 MIS 值覆蓋（缺 code 補列）。
    """
    now = time.time()
    rows = rows or []
    by_code = {str(r.get("code")): r for r in rows if r.get("code")}

    # 全域 tick 進展偵測：buffer 量總和有變 = 有新 tick
    vol_sum = sum(int(r.get("total_volume") or 0) for r in rows)
    if rows and vol_sum > 0 and vol_sum != _S["last_vol_sum"]:
        _S["last_vol_sum"] = vol_sum
        _S["last_tick_ts"] = now
    tick_age = round(now - _S["last_tick_ts"], 1) if _S["last_tick_ts"] else None

    market = _market_hours()
    feed_dead = market and (tick_age is None or tick_age > TICK_DEAD or not rows)
    aflow_stale = tick_age is None or tick_age > AFLOW_UNAVAIL

    if feed_dead:
        codes = _universe() or list(by_code.keys())
        mis = _poll_mis(codes)
        out = []
        for code in codes:
            r = by_code.get(code) or {"code": code}
            m = mis.get(code)
            if m and m.get("price") is not None:
                r["price"] = m["price"]
                r["change_rate"] = m["change_rate"]
                r["total_volume"] = int(m.get("total_volume") or 0)
                r["_price_source"] = "twse_mis"
                r["_quote_status"] = "BACKUP"
            else:
                r["_price_source"] = r.get("_price_source") or None
                r["_quote_status"] = "NO_DATA" if not r.get("price") else "STALE"
            r["_aflow_unavailable"] = True     # §1 行情死 → aflow 一律停用
            r["_last_tick_age"] = tick_age
            out.append(r)
        feed_state = "BACKUP"
        rows = out
    else:
        for r in rows:
            r["_price_source"] = "shioaji"
            r["_quote_status"] = "LIVE"
            r["_aflow_unavailable"] = bool(aflow_stale)   # 價量仍新，但 aflow 過期就停用
            r["_last_tick_age"] = tick_age
        feed_state = ("DEGRADED" if aflow_stale else "LIVE") if market else "CLOSED"

    meta = {
        "feed_state": feed_state,
        "last_tick_age": tick_age,
        "aflow_available": (feed_state == "LIVE"),
        "counts": {
            "total": len(rows),
            "quote_live": sum(1 for r in rows if r.get("_quote_status") == "LIVE"),
            "quote_backup": sum(1 for r in rows if r.get("_quote_status") == "BACKUP"),
        },
        "updated_at": _dt.datetime.now(_TW).isoformat(timespec="seconds"),
    }
    return rows, meta
