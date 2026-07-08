"""
MLS 標準版 — db.py
SQLite 資料層。schema 依交接規格書 v2 §3.1。
盤中即時寫入;盤後複查與學習迴圈讀取。
"""

import os
import json
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))
DB_PATH = os.path.join(os.path.dirname(__file__), "mls.db")
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS signals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT, trade_date TEXT,
          stock_id TEXT, stock_name TEXT, sector TEXT,
          event_class TEXT, action TEXT,
          triggered_rules TEXT,
          price REAL, change_rate REAL, volume_ratio REAL,
          suggested_stop REAL,
          confidence_label TEXT,
          is_watchlist_hit INTEGER DEFAULT 0,
          pushed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sector_snapshot(
          ts TEXT, trade_date TEXT, sector TEXT, sector_type TEXT,
          pct REAL, flow_score REAL, amount_share REAL,
          is_locked INTEGER, rank INTEGER
        );
        CREATE TABLE IF NOT EXISTS watchlist(
          trade_date TEXT, stock_id TEXT, stock_name TEXT,
          sector TEXT, reason TEXT,
          reverified INTEGER DEFAULT 0,   -- 08:55 開盤重驗
          demoted INTEGER DEFAULT 0,      -- 重驗降級(跳空破前低)
          hit INTEGER DEFAULT 0,          -- 收盤驗證:當日是否被鎖定/觸發
          PRIMARY KEY(trade_date, stock_id)
        );
        CREATE TABLE IF NOT EXISTS review_log(
          trade_date TEXT PRIMARY KEY,
          watch_total INTEGER, watch_hit INTEGER, hit_rate REAL,
          missed_stocks TEXT,             -- JSON:盤中強勢但不在清單
          notes TEXT
        );
        CREATE TABLE IF NOT EXISTS sector_daily(
          trade_date TEXT, sector TEXT,
          pct REAL,                        -- 收盤族群中位漲幅
          amount_share REAL,               -- 成交金額佔比
          flow_dir INTEGER,                -- 資金方向: 1流入 / -1流出(佔比 vs 前日)
          quadrant TEXT,                   -- in_up / in_down / out_down / out_up
          PRIMARY KEY(trade_date, sector)
        );
        CREATE TABLE IF NOT EXISTS factor_stats(
          trade_date TEXT, factor TEXT,
          triggered INTEGER, success INTEGER,
          PRIMARY KEY(trade_date, factor)
        );
        CREATE TABLE IF NOT EXISTS factor_weights(
          factor TEXT PRIMARY KEY, weight REAL, updated TEXT
        );
        CREATE TABLE IF NOT EXISTS kv(
          key TEXT PRIMARY KEY, value TEXT
        );
        CREATE TABLE IF NOT EXISTS signal_outcomes(
          trade_date TEXT, stock_id TEXT, signal_price REAL,
          close_price REAL, success INTEGER,
          PRIMARY KEY(trade_date, stock_id)
        );
        CREATE INDEX IF NOT EXISTS idx_sig_date ON signals(trade_date, stock_id);
        """)
        # 遷移:signals 加 factors 欄(舊庫無此欄時)
        try:
            c.execute("ALTER TABLE signals ADD COLUMN factors TEXT")
        except sqlite3.OperationalError:
            pass


def today():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


def now_iso():
    return datetime.now(TW_TZ).isoformat(timespec="seconds")


# ── 訊號 ──────────────────────────────────────────────
def insert_signal(sig, pushed=False):
    with _lock, _conn() as c:
        c.execute("""INSERT INTO signals
          (ts,trade_date,stock_id,stock_name,sector,event_class,action,
           triggered_rules,price,change_rate,volume_ratio,suggested_stop,
           confidence_label,is_watchlist_hit,pushed,factors)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (now_iso(), today(), sig["code"], sig["name"], sig["sector"],
           sig.get("event_class", ""), sig["action"],
           json.dumps(sig.get("rules", []), ensure_ascii=False),
           sig.get("price"), sig.get("change_rate"), sig.get("volume_ratio"),
           sig.get("suggested_stop"), sig.get("confidence_label"),
           1 if sig.get("is_watchlist_hit") else 0, 1 if pushed else 0,
           json.dumps(sig.get("factors", {}), ensure_ascii=False)))


def today_buy_signals():
    """今日進場訊號(首筆/每檔)供收盤成敗判定。"""
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT stock_id, MIN(ts) ts, price, factors FROM signals
               WHERE trade_date=? AND action='buy' GROUP BY stock_id""",
            (today(),))]


def record_factor_stats(rows):
    """rows: [{factor, triggered, success}] 累加至當日。"""
    with _lock, _conn() as c:
        for r in rows:
            c.execute("""INSERT INTO factor_stats VALUES(?,?,?,?)
              ON CONFLICT(trade_date,factor) DO UPDATE SET
              triggered=triggered+excluded.triggered,
              success=success+excluded.success""",
              (today(), r["factor"], r["triggered"], r["success"]))


def update_factor_weights(days=30):
    """30日移動窗格命中率 → 權重 w=clamp(0.5+hit,0.6,1.5)。回傳權重dict。"""
    with _lock, _conn() as c:
        rows = c.execute("""SELECT factor, SUM(triggered) t, SUM(success) s
          FROM factor_stats
          WHERE trade_date >= date('now','-{} day')
          GROUP BY factor""".format(int(days))).fetchall()
        out = {}
        for r in rows:
            if (r["t"] or 0) < 5:          # 樣本太少不調
                continue
            hit = r["s"] / r["t"]
            # 學習權重拉高:clamp 0.5~2.0;命中<45% 因子休眠(0.5)
            w = 0.5 if hit < 0.45 else min(2.0, max(0.5, 0.4 + hit * 1.2))
            out[r["factor"]] = round(w, 3)
            c.execute("INSERT OR REPLACE INTO factor_weights VALUES(?,?,?)",
                      (r["factor"], w, now_iso()))
        return out


def load_factor_weights():
    with _lock, _conn() as c:
        return {r["factor"]: r["weight"] for r in
                c.execute("SELECT * FROM factor_weights")}


def last_signal_ts(stock_id, action_group):
    """該股該事件群組最近一次已推播時間(冷卻用)。"""
    with _lock, _conn() as c:
        r = c.execute("""SELECT MAX(ts) m FROM signals
          WHERE stock_id=? AND trade_date=? AND pushed=1 AND action=?""",
          (stock_id, today(), action_group)).fetchone()
        return r["m"]


def signaled_today(stock_id):
    with _lock, _conn() as c:
        r = c.execute("""SELECT COUNT(*) n FROM signals
          WHERE stock_id=? AND trade_date=? AND action IN('buy','watch')""",
          (stock_id, today())).fetchone()
        return r["n"] > 0


# ── 族群快照 ──────────────────────────────────────────
def insert_sector_snapshot(sectors):
    with _lock, _conn() as c:
        for s in sectors:
            c.execute("""INSERT INTO sector_snapshot VALUES(?,?,?,?,?,?,?,?,?)""",
              (now_iso(), today(), s["name"], s["type"], s["pct"],
               s["flow_score"], s["amount_share"],
               1 if s["locked"] else 0, s["rank"]))


# ── 觀察清單 ──────────────────────────────────────────
def save_watchlist(trade_date, rows):
    with _lock, _conn() as c:
        for r in rows:
            c.execute("""INSERT OR REPLACE INTO watchlist
              (trade_date,stock_id,stock_name,sector,reason)
              VALUES(?,?,?,?,?)""",
              (trade_date, r["code"], r["name"], r["sector"], r["reason"]))


def load_watchlist(trade_date):
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM watchlist WHERE trade_date=?", (trade_date,))]


def mark_reverify(trade_date, stock_id, demoted):
    with _lock, _conn() as c:
        c.execute("""UPDATE watchlist SET reverified=1, demoted=?
          WHERE trade_date=? AND stock_id=?""",
          (1 if demoted else 0, trade_date, stock_id))


def mark_watch_hit(trade_date, stock_id):
    with _lock, _conn() as c:
        c.execute("""UPDATE watchlist SET hit=1
          WHERE trade_date=? AND stock_id=?""", (trade_date, stock_id))


# ── 收盤驗證 ──────────────────────────────────────────
def write_review(trade_date, total, hit, missed, notes=""):
    rate = round(hit / total * 100, 1) if total else 0.0
    with _lock, _conn() as c:
        c.execute("""INSERT OR REPLACE INTO review_log VALUES(?,?,?,?,?,?)""",
          (trade_date, total, hit, rate,
           json.dumps(missed, ensure_ascii=False), notes))
    return rate


def recent_hit_rates(days=30):
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT trade_date, hit_rate FROM review_log
               ORDER BY trade_date DESC LIMIT ?""", (days,))]


def save_sector_daily(trade_date, rows):
    """rows: [{sector, pct, amount_share, flow_dir, quadrant}]"""
    with _lock, _conn() as c:
        for r in rows:
            c.execute("INSERT OR REPLACE INTO sector_daily VALUES(?,?,?,?,?,?)",
                      (trade_date, r["sector"], r["pct"], r["amount_share"],
                       r["flow_dir"], r["quadrant"]))


def sector_history(sector, days=6):
    """回傳該族群最近 N 個交易日紀錄(舊→新)。"""
    with _lock, _conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM sector_daily WHERE sector=?
               ORDER BY trade_date DESC LIMIT ?""", (sector, days))]
    return list(reversed(rows))


def prev_amount_share(sector, today=None):
    """取該族群前一個交易日的 amount_share(排除今日)。"""
    with _lock, _conn() as c:
        if today is None:
            today = c.execute("SELECT trade_date FROM sector_daily "
                              "ORDER BY trade_date DESC LIMIT 1").fetchone()
            today = today["trade_date"] if today else None
        if today:
            r = c.execute("""SELECT amount_share FROM sector_daily
              WHERE sector=? AND trade_date<?
              ORDER BY trade_date DESC LIMIT 1""", (sector, today)).fetchone()
        else:
            r = c.execute("""SELECT amount_share FROM sector_daily
              WHERE sector=? ORDER BY trade_date DESC LIMIT 1""", (sector,)).fetchone()
        return r["amount_share"] if r else None


def today_stats():
    with _lock, _conn() as c:
        sig = c.execute("""SELECT
            COUNT(*) total,
            SUM(CASE WHEN action='buy' THEN 1 ELSE 0 END) buys,
            SUM(CASE WHEN action='sell' THEN 1 ELSE 0 END) risks
          FROM signals WHERE trade_date=?""", (today(),)).fetchone()
        return dict(sig)


# ── KV 與精度統計(80%準度控制器 / 回撤斷路器共用) ────────
def kv_get(key, default=None):
    with _lock, _conn() as c:
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def kv_set(key, value):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, str(value)))


def record_outcomes(rows):
    """rows: [{stock_id, signal_price, close_price, success}]"""
    with _lock, _conn() as c:
        for r in rows:
            c.execute("INSERT OR REPLACE INTO signal_outcomes VALUES(?,?,?,?,?)",
                      (today(), r["stock_id"], r["signal_price"],
                       r["close_price"], 1 if r["success"] else 0))


def rolling_precision(days=10):
    """近N日進場訊號精度。回傳 (precision or None, n)。"""
    with _lock, _conn() as c:
        r = c.execute("""SELECT COUNT(*) n, SUM(success) s FROM signal_outcomes
          WHERE trade_date >= date('now','-{} day')""".format(int(days))).fetchone()
        n = r["n"] or 0
        return ((r["s"] or 0) / n if n else None), n
