# -*- coding: utf-8 -*-
"""盤後分類蓋章；獨立表，不碰盤中狀態。"""

import datetime as _dt
import sqlite3
from typing import List, Optional

from .classify import classify_one
from .intraday_filter import (
    StockSnap, aflow_intensity, market_regime, passes_filters, proxy_quadrant,
)

DDL = """
CREATE TABLE IF NOT EXISTS intraday_eod (
    trade_date TEXT NOT NULL, code TEXT NOT NULL, close_price REAL,
    change_rate REAL, aflow INTEGER, aflow_intensity REAL, quadrant TEXT,
    regime TEXT, group_name TEXT, subgroup TEXT, all_pass INTEGER,
    extreme_price INTEGER, signal_reliable INTEGER,
    data_stage TEXT DEFAULT 'eod_stamped', stamped_at TEXT,
    PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_eod_date ON intraday_eod(trade_date);
CREATE INDEX IF NOT EXISTS idx_eod_group ON intraday_eod(trade_date, group_name);
"""


def ensure_table(conn: sqlite3.Connection):
    conn.executescript(DDL)
    conn.commit()


def stamp_one(conn: sqlite3.Connection, snap: StockSnap, regime: str,
              trade_date: Optional[str] = None):
    day = trade_date or _dt.date.today().strftime("%Y-%m-%d")
    classification = classify_one(snap, regime=regime)
    filters = passes_filters(snap, regime=regime)
    conn.execute(
        """INSERT OR REPLACE INTO intraday_eod
        (trade_date, code, close_price, change_rate, aflow, aflow_intensity,
         quadrant, regime, group_name, subgroup, all_pass, extreme_price,
         signal_reliable, data_stage, stamped_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'eod_stamped',?)""",
        (day, snap.code, snap.price, snap.change_rate, snap.aflow,
         aflow_intensity(snap.aflow, snap.total_volume),
         proxy_quadrant(snap.aflow, snap.change_rate), regime,
         classification["group"], classification["subgroup"],
         int(filters["all_pass"]), int(filters["extreme"]),
         int(not filters["extreme"]),
         _dt.datetime.now().isoformat(timespec="seconds")),
    )


def run_eod_stamp(db_path: str, snaps: List[StockSnap], thermometer_score: int,
                  trade_date: Optional[str] = None) -> dict:
    day = trade_date or _dt.date.today().strftime("%Y-%m-%d")
    regime = market_regime(thermometer_score)
    with sqlite3.connect(db_path) as conn:
        ensure_table(conn)
        for snap in snaps:
            stamp_one(conn, snap, regime, day)
        count = conn.execute(
            "SELECT COUNT(*) FROM intraday_eod WHERE trade_date=?", (day,)
        ).fetchone()[0]
    return {"trade_date": day, "stamped": count, "regime": regime}


def load_eod(db_path: str, trade_date: str,
             group_name: Optional[str] = None) -> List[dict]:
    sql = "SELECT * FROM intraday_eod WHERE trade_date=?"
    args = [trade_date]
    if group_name:
        sql += " AND group_name=?"
        args.append(group_name)
    sql += " ORDER BY aflow DESC"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, args).fetchall()]


def load_stock_history(db_path: str, code: str, days: int = 20) -> List[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT trade_date, code, close_price, change_rate, aflow, "
            "aflow_intensity, quadrant, group_name, subgroup, all_pass, "
            "signal_reliable FROM intraday_eod WHERE code=? "
            "ORDER BY trade_date DESC LIMIT ?", (code, days)
        ).fetchall()
        return [dict(row) for row in reversed(rows)]


def list_trade_dates(db_path: str, limit: int = 60) -> List[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM intraday_eod "
            "ORDER BY trade_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [row[0] for row in rows]


def stock_trend_summary(db_path: str, code: str, days: int = 5) -> dict:
    history = load_stock_history(db_path, code, days)
    if not history:
        return {"code": code, "trend": "無資料", "detail": ""}
    groups = [row["group_name"] for row in history]
    rank = {"可操作": 2, "觀察": 1, "排除": 0}
    scores = [rank.get(group, 0) for group in groups]
    if all(group == "可操作" for group in groups):
        trend = "持續可操作"
    elif scores[-1] > scores[0]:
        trend = "分類爬升"
    elif scores[-1] < scores[0]:
        trend = "分類惡化"
    else:
        trend = "分類反覆"
    return {"code": code, "trend": trend,
            "detail": " → ".join(groups), "days": len(history),
            "latest_group": groups[-1]}
