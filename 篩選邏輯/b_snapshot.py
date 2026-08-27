"""
b_snapshot.py — B 鏈:盤中時序快照

⚠ 這是 B 鏈的一部分,與 A 鏈完全獨立。
   B 鏈不讀 A 鏈的候選池,A 鏈不讀 B 鏈的任何表。
   兩條鏈唯一的交會點在盤後匯流(merge_pool.py),其餘時候互不相干。
   這支整支爆掉,A 鏈的盤中燈號照常運作。

任務:每 5 分鐘把記憶體 buffer 裡的數字寫一筆進 SQLite,累積出時序。

===== 零 API 呼叫(這條最重要) =====

Shioaji 是訂閱推送:你訂閱一次,它主動把每筆成交推給你。
所以記憶體裡隨時都有最新價量,不需要去「拉」。

    Shioaji 持續推送 ──→ 記憶體 buffer(隨時更新)
                              ↓ 每 5 分鐘
                         寫一筆進 SQLite  ← 這步不碰網路

這支程式裡不會出現任何 Shioaji 呼叫。它只讀你傳進來的 dict、只寫 DB。
所以快照模組跟永豐那條線完全脫鉤,這支爆掉不影響訂閱。

絕對禁止:改成「每 5 分鐘 call Shioaji snapshot API 拉一次」。
那就是輪詢,違反「Shioaji 只用訂閱、盤中永不 polling」的既有規則。

負載:51 檔 × 54 個時點 ≈ 2,700 筆/日。SQLite 無感。
"""

from __future__ import annotations

import datetime as _dt

import store
from phase import Phase, get_phase, now_tw, today_tw

PLUGIN = "b_snapshot"
TABLE = "b_snapshot"

SLOT_MINUTES = 5
OPEN_H, OPEN_M = 9, 0
CLOSE_H, CLOSE_M = 13, 30


def slot_of(at: _dt.datetime | None = None) -> str:
    """把時間對齊到 5 分鐘格。09:07 → '0905'。"""
    at = at or now_tw()
    m = (at.minute // SLOT_MINUTES) * SLOT_MINUTES
    return f"{at.hour:02d}{m:02d}"


def minutes_since_open(at: _dt.datetime | None = None) -> int:
    at = at or now_tw()
    return (at.hour - OPEN_H) * 60 + (at.minute - OPEN_M)


def take(buffer: dict[str, dict], at: _dt.datetime | None = None,
         db_path: str = "mls.db") -> dict:
    """
    寫一筆快照。

    buffer 是你的 Shioaji 訂閱 handler 維護的記憶體 dict,格式:
        { "2330": {"price":..., "change_rate":..., "volume":...,
                   "net_active":..., "bid_vol":..., "ask_vol":...,
                   "quote_updated_at":..., "aflow_updated_at":...,
                   "freshness_gap_sec":..., "aflow_method":...}, ... }
    後四個 freshness 欄位純觀察用(見 snapshot_producer.build_buffer 註解),
    缺值就是 None,不用任何門檻頂替或篩掉。

    這支不呼叫 Shioaji,只把你已經有的東西落地。
    """
    at = at or now_tw()
    ph = get_phase(at)
    if ph is not Phase.INTRADAY:
        # 非盤中不寫。不是錯誤,是這支沒事做。
        return {"written": 0, "slot": None, "skipped": f"phase={ph.value}"}

    slot = slot_of(at)
    d = today_tw().isoformat()
    now = at.isoformat(timespec="seconds")

    rows = [{
        "data_date": d, "code": code, "slot": slot,
        "price": v.get("price"), "change_rate": v.get("change_rate"),
        "volume": v.get("volume"), "net_active": v.get("net_active"),
        "bid_vol": v.get("bid_vol"), "ask_vol": v.get("ask_vol"),
        "created_at": now,
        "quote_updated_at": v.get("quote_updated_at"),
        "aflow_updated_at": v.get("aflow_updated_at"),
        "freshness_gap_sec": v.get("freshness_gap_sec"),
        "aflow_method": v.get("aflow_method"),
    } for code, v in buffer.items()]

    n = store.upsert_intraday(TABLE, PLUGIN, rows, db_path)
    return {"written": n, "slot": slot, "api_calls": 0}


def series(code: str, data_date: _dt.date | None = None,
           db_path: str = "mls.db") -> list[dict]:
    """取某檔當日的完整時序,依 slot 排序。"""
    d = (data_date or today_tw()).isoformat()
    with store.conn(db_path) as c:
        rows = c.execute(
            "SELECT * FROM b_snapshot WHERE data_date=? AND code=? ORDER BY slot",
            (d, code),
        ).fetchall()
    return [dict(r) for r in rows]


def series_all(data_date: _dt.date | None = None,
               db_path: str = "mls.db") -> dict[str, list[dict]]:
    """取當日全部標的的時序。{code: [snapshot, ...]}"""
    d = (data_date or today_tw()).isoformat()
    with store.conn(db_path) as c:
        rows = c.execute(
            "SELECT * FROM b_snapshot WHERE data_date=? ORDER BY code, slot", (d,)
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["code"], []).append(dict(r))
    return out


def coverage(data_date: _dt.date | None = None, db_path: str = "mls.db") -> dict:
    """診斷:今天存了幾個時點、幾檔。用來確認快照有在跑。"""
    d = (data_date or today_tw()).isoformat()
    with store.conn(db_path) as c:
        r = c.execute(
            "SELECT COUNT(DISTINCT slot) slots, COUNT(DISTINCT code) codes,"
            " COUNT(*) rows FROM b_snapshot WHERE data_date=?", (d,)
        ).fetchone()
    return dict(r)
