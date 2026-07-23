# -*- coding: utf-8 -*-
"""
eod_stamp.py — 收盤蓋章：把當日盤中最終結果存一份盤後歷史

鐵律（對齊系統「盤中估值 vs 盤後蓋章值硬分離」）：
  - 獨立表 intraday_eod，不碰 stock_state（盤中即時暫存）、不碰主站 STATE。
  - data_stage='eod_stamped' 標記收盤定案，與盤中 'intraday_est' 區分。
  - 每天每檔一筆，主鍵 (trade_date, code)；同日重跑可更新，不同日各自獨立。
  - 極端價（跌停鎖死等）照實存但標 signal_reliable=False，歷史要完整不丟資料。

觸發：APScheduler 13:30 收盤後呼叫 run_eod_stamp() 一次。
"""

import sqlite3
import datetime as _dt
from typing import List, Optional
from .intraday_filter import (
    StockSnap, passes_filters, proxy_quadrant, aflow_intensity, market_regime,
)
from .classify import classify_one


DDL = """
CREATE TABLE IF NOT EXISTS intraday_eod (
    trade_date      TEXT NOT NULL,          -- YYYY-MM-DD
    code            TEXT NOT NULL,
    close_price     REAL,
    change_rate     REAL,
    aflow           INTEGER,                -- 收盤定案 aflow
    aflow_intensity REAL,                   -- 吸籌強度佔比 %
    quadrant        TEXT,                   -- 收盤象限
    regime          TEXT,                   -- 當日盤勢
    group_name      TEXT,                   -- 分類：可操作/觀察/排除
    subgroup        TEXT,                   -- 子分類
    all_pass        INTEGER,                -- 1/0
    extreme_price   INTEGER,                -- 1/0
    signal_reliable INTEGER,                -- 極端價 → 0
    data_stage      TEXT DEFAULT 'eod_stamped',
    stamped_at      TEXT,                   -- 蓋章時間戳
    PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_eod_date ON intraday_eod(trade_date);
CREATE INDEX IF NOT EXISTS idx_eod_group ON intraday_eod(trade_date, group_name);
"""


def ensure_table(conn: sqlite3.Connection):
    conn.executescript(DDL)
    conn.commit()


def stamp_one(conn: sqlite3.Connection, s: StockSnap, regime: str,
              trade_date: Optional[str] = None):
    """單檔蓋章寫入。同 (date, code) 重跑會更新當日（INSERT OR REPLACE）。"""
    trade_date = trade_date or _dt.date.today().strftime("%Y-%m-%d")
    c = classify_one(s, regime=regime)
    filt = passes_filters(s, regime=regime)
    reliable = 0 if filt["extreme"] else 1

    conn.execute("""
        INSERT OR REPLACE INTO intraday_eod
        (trade_date, code, close_price, change_rate, aflow, aflow_intensity,
         quadrant, regime, group_name, subgroup, all_pass, extreme_price,
         signal_reliable, data_stage, stamped_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'eod_stamped', ?)
    """, (
        trade_date, s.code, s.price, s.change_rate, s.aflow,
        aflow_intensity(s.aflow, s.total_volume),
        proxy_quadrant(s.aflow, s.change_rate), regime,
        c["group"], c["subgroup"], 1 if filt["all_pass"] else 0,
        1 if filt["extreme"] else 0, reliable,
        _dt.datetime.now().isoformat(timespec="seconds"),
    ))


def run_eod_stamp(db_path: str, snaps: List[StockSnap], thermometer_score: int,
                  trade_date: Optional[str] = None) -> dict:
    """
    收盤蓋章主流程（13:30 觸發一次）。
    snaps: 當日收盤最終快照（各檔最後一筆盤中值）。
    回傳 {trade_date, stamped, regime}。
    """
    regime = market_regime(thermometer_score)
    conn = sqlite3.connect(db_path)
    try:
        ensure_table(conn)
        for s in snaps:
            stamp_one(conn, s, regime, trade_date)
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM intraday_eod WHERE trade_date = ?",
            (trade_date or _dt.date.today().strftime("%Y-%m-%d"),),
        ).fetchone()[0]
    finally:
        conn.close()
    return {"trade_date": trade_date or _dt.date.today().strftime("%Y-%m-%d"),
            "stamped": n, "regime": regime}


def load_eod(db_path: str, trade_date: str,
             group_name: Optional[str] = None) -> List[dict]:
    """讀某日盤後歷史，可選按分類群過濾。單日回看用。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if group_name:
            rows = conn.execute(
                "SELECT * FROM intraday_eod WHERE trade_date=? AND group_name=? "
                "ORDER BY aflow DESC", (trade_date, group_name)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM intraday_eod WHERE trade_date=? ORDER BY aflow DESC",
                (trade_date,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_stock_history(db_path: str, code: str, days: int = 20) -> List[dict]:
    """
    跨日追蹤：某檔近 N 個交易日的分類/aflow 變化。
    回傳依日期由舊到新，供畫「分類軌跡 / aflow 走勢」——
    連續留在可操作、或從排除爬升到可操作，是單日快照看不到的訊號。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT trade_date, code, close_price, change_rate, aflow, "
            "aflow_intensity, quadrant, group_name, subgroup, all_pass, "
            "signal_reliable FROM intraday_eod WHERE code=? "
            "ORDER BY trade_date DESC LIMIT ?", (code, days)).fetchall()
        return [dict(r) for r in reversed(rows)]   # 由舊到新
    finally:
        conn.close()


def list_trade_dates(db_path: str, limit: int = 60) -> List[str]:
    """列出有資料的交易日（日期選擇器用），由新到舊。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM intraday_eod "
            "ORDER BY trade_date DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def stock_trend_summary(db_path: str, code: str, days: int = 5) -> dict:
    """
    跨日訊號摘要：近 N 日分類軌跡濃縮成一句可判讀的結論。
    連續可操作 / 爬升 / 惡化 / 反覆 —— 給歷史頁個股展開的頂部提示。
    """
    hist = load_stock_history(db_path, code, days)
    if not hist:
        return {"code": code, "trend": "無資料", "detail": ""}
    groups = [h["group_name"] for h in hist]
    rank = {"可操作": 2, "觀察": 1, "排除": 0}
    scores = [rank.get(g, 0) for g in groups]

    if all(g == "可操作" for g in groups):
        trend = "持續可操作"
    elif scores[-1] > scores[0]:
        trend = "分類爬升"
    elif scores[-1] < scores[0]:
        trend = "分類惡化"
    else:
        trend = "分類反覆"
    detail = " → ".join(groups)
    return {"code": code, "trend": trend, "detail": detail,
            "days": len(hist), "latest_group": groups[-1]}
