"""line_b_audit_log.py — Line B 即時頁「當時顯示了什麼」的 append-only 記錄。

2026-08-27 新增(Vanessa 要求第 4 點):每次即時頁渲染,把當時輸入
(price/net_active/distance_pct/confirmed_so_far/是否 stale)與顯示結果
(status/activation_prob/calibration_bucket/calibration_version)整批寫進來,
純追加、不做任何 dedup/no-op 判斷——這是「觀測紀錄」不是「凍結事實」,
每一次渲染都是一筆獨立觀測,重複本身就是有意義的資訊(證明那個時間點真的
被看過那個數字)。

不影響任何既有 tier/track/score,不讀 Line A 任何表。
"""
from __future__ import annotations

import datetime as _dt
import sqlite3

TABLE = "line_b_prob_audit"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,       -- 這筆記錄寫入當下(naive datetime.now(),
                                    -- 跟 feed updated_at 同一種時鐘,方便對比)
    data_date TEXT NOT NULL,       -- T(這一列描述的交易日)
    code TEXT NOT NULL,
    source TEXT,                   -- C1C2_PASS / INTRADAY_DISCOVERY
    status TEXT,                   -- WAIT/WATCH_CLOSELY/CONFIRMED/GIVE_UP
    calibration_version TEXT,
    calibration_bucket TEXT,
    confirmed_so_far INTEGER,
    distance_pct REAL,
    current_price REAL,
    resistance REAL,
    net_active REAL,
    flow_stale INTEGER,
    activation_prob REAL
);
CREATE INDEX IF NOT EXISTS idx_line_b_prob_audit_code_date
    ON {TABLE}(code, data_date);
"""


def ensure(db_path: str = "mls.db") -> None:
    with sqlite3.connect(db_path) as c:
        c.executescript(DDL)


def log_rows(data_date: str, rows: list[dict], db_path: str = "mls.db") -> int:
    """rows: line_b_live.build_live_rows()['rows'] 的元素(已含 'explain')。
    純追加,不做任何覆寫/no-op 判斷。回傳寫入筆數。"""
    if not rows:
        return 0
    ensure(db_path)
    now = _dt.datetime.now().isoformat(timespec="seconds")
    payload = []
    for r in rows:
        exp = r.get("explain") or {}
        payload.append((
            now, data_date, r.get("code"), r.get("source"), exp.get("status"),
            exp.get("calibration_version"), exp.get("calibration_bucket"),
            int(bool(exp.get("confirmed_so_far"))) if exp.get("confirmed_so_far") is not None else None,
            exp.get("distance_pct"), exp.get("current"), exp.get("resistance"),
            r.get("flow_confirm_magnitude"),
            int(bool(exp.get("flow_stale"))), exp.get("activation_prob"),
        ))
    with sqlite3.connect(db_path) as c:
        c.executemany(
            f"""INSERT INTO {TABLE} (logged_at, data_date, code, source, status,
                calibration_version, calibration_bucket, confirmed_so_far,
                distance_pct, current_price, resistance, net_active, flow_stale,
                activation_prob) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            payload,
        )
        c.commit()
    return len(payload)
