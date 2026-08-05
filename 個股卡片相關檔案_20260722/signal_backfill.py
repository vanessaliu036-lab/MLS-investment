"""
signal_backfill.py — 一次性回填「昨日訊號型態 + 觸發原因」到歷史名單/驗證。

為什麼要它:型態在「選股當下」算、原因在「T+1 驗證當下」算,所以部署前既有的
watchlist / watch_outcome 這兩欄是空的,B 卡歷史日看不到型態與明確原因。這支用
歷史日K把過去幾天回補齊,讓 B 卡立刻顯示,不用等下一輪 pick→verify 自然長出。

⚠ 必須跑在「服務行程內」(經 /api 端點呼叫),不可另開行程 —— broker.daily_kbars
   會登入 Shioaji,獨立行程用同金鑰重登會踢掉線上行情 session(session 互踢 bug)。

只 UPDATE 新欄位(signal_type/trigger_price/trigger_status/non_trigger_reason),
不動 verdict / hit / change_rate / 命中率 / 報酬。跑前自動備份兩張表。
"""

from __future__ import annotations

import datetime as _dt

import broker
import db
import signal_pattern


def _bars_by_date(code: str, days: int = 90):
    """回 {date: bar} 與 [dates 舊→新]。bar 含 ts/close/high/low/open/volume。"""
    bars = broker.daily_kbars(str(code), days=days) or []
    idx, order = {}, []
    for b in bars:
        d = b.get("ts")
        if isinstance(d, _dt.datetime):
            d = d.date()
        if d is None:
            continue
        idx[d] = b
        order.append(d)
    return idx, order


def _slice_upto(bars_order, bars_idx, upto: _dt.date):
    """取 upto(含)以前的日K序列(舊→新),供 classify 用當時視角判型態。"""
    return [bars_idx[d] for d in bars_order if d <= upto]


def run(days: int = 20) -> dict:
    """回填最近 days 個交易日內的 watchlist / watch_outcome。回統計。"""
    with db._lock, db._conn() as c:
        # 備份(冪等:先刪舊備份再建)
        for t in ("watchlist", "watch_outcome"):
            c.execute(f"DROP TABLE IF EXISTS {t}_bak_signalfill")
            c.execute(f"CREATE TABLE {t}_bak_signalfill AS SELECT * FROM {t}")
        since = (db.datetime.now(db.TW_TZ).date() - _dt.timedelta(days=days * 2)).isoformat()
        wl_rows = [dict(r) for r in c.execute(
            "SELECT * FROM watchlist WHERE trade_date>=? ORDER BY trade_date", (since,))]
        oc_rows = [dict(r) for r in c.execute(
            "SELECT * FROM watch_outcome WHERE trade_date>=? ORDER BY trade_date", (since,))]

    kb_cache: dict[str, tuple] = {}

    def _kb(code):
        if code not in kb_cache:
            kb_cache[code] = _bars_by_date(code)
        return kb_cache[code]

    def prev_trading(order, d):
        prev = [x for x in order if x < d]
        return prev[-1] if prev else None

    wl_done = wl_skip = 0
    wl_updates = []
    for w in wl_rows:
        code, td = str(w["stock_id"]), w["trade_date"]
        try:
            idx, order = _kb(code)
            tdate = _dt.date.fromisoformat(td)
            pick_day = prev_trading(order, tdate)      # 名單於前一交易日晚間選出
            if pick_day is None:
                wl_skip += 1
                continue
            series = _slice_upto(order, idx, pick_day)
            r = signal_pattern.classify(series)
            if r.get("signal_type"):
                wl_updates.append((r["signal_type"], r.get("trigger_price"), td, code))
                wl_done += 1
            else:
                # 無具名型態:仍補預設觸發價(radar→昨高、resilient→月線),讓驗證有明確原因
                kind = signal_pattern.kind_of(None, w.get("source"))
                tp = signal_pattern.default_trigger(series, kind)
                if tp is not None:
                    wl_updates.append((None, tp, td, code))
                wl_skip += 1
        except Exception:
            wl_skip += 1

    oc_done = oc_skip = 0
    oc_updates = []
    # 建 watchlist 型態查表(供 outcome 取 kind/trigger)
    wl_map = {(w["trade_date"], str(w["stock_id"])): w for w in wl_rows}
    wl_backfilled = {(td, code): (stype, tp) for (stype, tp, td, code) in wl_updates}
    for o in oc_rows:
        code, td = str(o["stock_id"]), o["trade_date"]
        try:
            stype, tp = wl_backfilled.get((td, code), (None, None))
            if tp is None:                             # 這批沒補到觸發價 → 讀名單既有值
                w = wl_map.get((td, code)) or {}
                tp = w.get("trigger_price")
                stype = stype or w.get("signal_type")
            source = (wl_map.get((td, code)) or {}).get("source")
            kind = signal_pattern.kind_of(stype, source)
            idx, _order = _kb(code)
            bar = idx.get(_dt.date.fromisoformat(td)) or {}
            trig, why = signal_pattern.describe_trigger(
                kind, tp,
                today_high=bar.get("high"), today_low=bar.get("low"),
                today_close=bar.get("close"), chg=o.get("change_rate"),
                volume_ratio=o.get("volume_ratio"), aflow=o.get("aflow"))
            oc_updates.append((stype, tp, trig, why, td, code))
            oc_done += 1
        except Exception:
            oc_skip += 1

    with db._lock, db._conn() as c:
        c.executemany(
            "UPDATE watchlist SET signal_type=?, trigger_price=? WHERE trade_date=? AND stock_id=?",
            wl_updates)
        c.executemany(
            """UPDATE watch_outcome SET signal_type=?, trigger_price=?,
                      trigger_status=?, non_trigger_reason=? WHERE trade_date=? AND stock_id=?""",
            oc_updates)

    return {
        "backup": "watchlist_bak_signalfill / watch_outcome_bak_signalfill",
        "watchlist": {"updated": wl_done, "skipped": wl_skip, "total": len(wl_rows)},
        "watch_outcome": {"updated": oc_done, "skipped": oc_skip, "total": len(oc_rows)},
        "codes_fetched": len(kb_cache),
    }
