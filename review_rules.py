# -*- coding: utf-8 -*-
"""盤後驗證（自動版）— 源自「盤後驗證.py」的一鍵 CSV 腳本。

比對「盤中決策」與「實際走勢」，累積命中率資料，找出哪些規則準、哪些不準。
差別在於全自動：
  1. 盤中 /api/intraday-test 每輪把「每檔首次進入某分類」的訊號價記進 SQLite，
     並持續更新最後成交價（13:30 收盤定盤價自然落在 last_price）。
  2. 收盤後第一次讀 /api/review/rules 時自動判定命中/未命中並累積入庫，
     永不覆蓋舊資料；天數越多統計越準。
  3. 不打任何外部 API、不寫 mls.db；資料落在獨立的 intraday_eod.db。

分類對應：原腳本用 可進場/等待突破/風險警報；本系統盤中實際分類是
可操作/觀察/排除，規則語意一對一對應（可操作=可進場、排除=風險警報）。
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "intraday_eod.db"   # 測試可覆寫
TW_TZ = ZoneInfo("Asia/Taipei")

# ------------------ 判定規則（想調整靈敏度只改這裡的數字就好） ------------------
RULES = {
    "可操作": {"type": "up",       "threshold": 0.015,  "desc": "收盤漲幅 >= 1.5% 視為命中"},
    "觀察":   {"type": "breakout", "threshold": 0.0,    "desc": "收盤價 > 訊號價 視為命中"},
    "排除":   {"type": "down",     "threshold": -0.015, "desc": "收盤跌幅 <= -1.5% 視為命中"},
}
# --------------------------------------------------------------------------------

# 台股 13:30 收盤；13:33 後 last_price 已含定盤價，才允許判定今日。
VALIDATE_AFTER = (13, 33)
# 只在盤中建立新訊號，避免收盤後 buffer 殘留把收盤價記成訊號價。
SIGNAL_WINDOW = ((8, 55), (13, 30))


def _now():
    return datetime.now(TW_TZ)


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS rule_signals(
        trade_date   TEXT NOT NULL,
        code         TEXT NOT NULL,
        name         TEXT,
        category     TEXT NOT NULL,
        subgroup     TEXT,
        aflow        INTEGER,
        signal_price REAL,
        signal_ts    TEXT,
        last_price   REAL,
        result       TEXT,
        validated_at TEXT,
        PRIMARY KEY (trade_date, code, category))""")
    # 遷移：T+1 隔日驗證欄（Phase 3 生效）。SQLite ADD COLUMN 無可靠
    # IF NOT EXISTS，先查 PRAGMA 再加，避免每次連線重跑報 duplicate column。
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(rule_signals)")}
    for col, decl in (("next_close", "REAL"), ("next_result", "TEXT"),
                      ("next_relative_sector", "REAL"), ("verified_at", "TEXT")):
        if col not in existing:
            conn.execute(f"ALTER TABLE rule_signals ADD COLUMN {col} {decl}")
    return conn


def judge(category, signal_price, close_price):
    """與原腳本 judge() 同語意；回傳 命中/未命中/資料不足/無規則。"""
    rule = RULES.get(str(category or "").strip())
    if rule is None:
        return "無規則"
    try:
        signal_price = float(signal_price)
        close_price = float(close_price)
    except (ValueError, TypeError):
        return "資料不足"
    if signal_price <= 0:
        return "資料不足"

    change = (close_price - signal_price) / signal_price
    if rule["type"] == "up":
        return "命中" if change >= rule["threshold"] else "未命中"
    if rule["type"] == "down":
        return "命中" if change <= rule["threshold"] else "未命中"
    if rule["type"] == "breakout":
        return "命中" if close_price > signal_price else "未命中"
    return "無規則"


def record(rows, now=None):
    """盤中每輪呼叫：首次進入分類→記訊號價；既有訊號→更新最後價。

    只吃即時 buffer 的 rows（呼叫端負責過濾快照/回退來源）。"""
    now = now or _now()
    day = now.date().isoformat()
    hm = (now.hour, now.minute)
    in_session = SIGNAL_WINDOW[0] <= hm <= SIGNAL_WINDOW[1]
    with _conn() as conn:
        for row in rows:
            code = str(row.get("code") or "")
            category = str(row.get("group") or "")
            price = row.get("price")
            if not code or category not in RULES or not price:
                continue
            if in_session:
                conn.execute(
                    """INSERT INTO rule_signals
                       (trade_date, code, name, category, subgroup, aflow,
                        signal_price, signal_ts, last_price)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(trade_date, code, category)
                       DO UPDATE SET last_price=excluded.last_price""",
                    (day, code, row.get("name"), category, row.get("subgroup"),
                     row.get("aflow"), float(price), now.strftime("%H:%M:%S"),
                     float(price)))
            else:
                # 收盤後殘留 buffer：只更新今日既有訊號的收盤價，不開新訊號。
                conn.execute(
                    """UPDATE rule_signals SET last_price=?
                       WHERE trade_date=? AND code=? AND category=?""",
                    (float(price), day, code, category))


def validate(now=None):
    """判定所有未驗證訊號：過去交易日一律判；今日需過 13:33。"""
    now = now or _now()
    today = now.date().isoformat()
    today_ready = (now.hour, now.minute) >= VALIDATE_AFTER
    validated = 0
    with _conn() as conn:
        pending = conn.execute(
            "SELECT * FROM rule_signals WHERE result IS NULL").fetchall()
        for row in pending:
            if row["trade_date"] == today and not today_ready:
                continue
            result = judge(row["category"], row["signal_price"], row["last_price"])
            conn.execute(
                """UPDATE rule_signals SET result=?, validated_at=?
                   WHERE trade_date=? AND code=? AND category=?""",
                (result, now.isoformat(),
                 row["trade_date"], row["code"], row["category"]))
            validated += 1
    return validated


def api_payload(now=None):
    """/api/review/rules 回應：先補驗證，再回累積命中率與今日明細。"""
    now = now or _now()
    validate(now)
    today = now.date().isoformat()
    with _conn() as conn:
        summary = [dict(r) for r in conn.execute(
            """SELECT category, COUNT(*) total,
                      SUM(result='命中') hits,
                      ROUND(SUM(result='命中')*100.0/COUNT(*), 1) hit_rate
               FROM rule_signals WHERE result IN ('命中','未命中')
               GROUP BY category""")]
        days = conn.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM rule_signals").fetchone()[0]
        today_rows = [dict(r) for r in conn.execute(
            """SELECT * FROM rule_signals WHERE trade_date=?
               ORDER BY category, code""", (today,))]
    for item in summary:
        item["rule"] = RULES.get(item["category"], {}).get("desc", "")
    for item in today_rows:
        if item["result"] is None:
            item["result"] = "待驗證"
        sp, lp = item.get("signal_price"), item.get("last_price")
        item["change_rate"] = (round((lp - sp) / sp * 100, 2)
                               if sp and lp else None)
    return {
        "ok": True, "trade_date": today, "days": days,
        "summary": summary, "today": today_rows,
        "rules": {k: v["desc"] for k, v in RULES.items()},
        "note": "訊號價=盤中首次進入該分類；收盤價=當日最後成交價；13:33 後自動判定並永久累積",
    }
