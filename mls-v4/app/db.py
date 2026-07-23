"""
MLS v4.0 — db.py
SQLite 封裝。所有模組只透過這裡讀寫狀態表（拼圖架構的「底板」）。
崩潰重啟後，狀態從 DB 還原，資訊永不歸零。

狀態表：
  stock_state    每檔盤中即時狀態（intraday_est），狀態轉移即寫入
  dec_health     每日盤後蓋章健康度（eod_final）
  health_daily   健康度時間序列（連續天數/趨勢/象限歷史）
  dec_watchlist  每日觀察清單（明日標的）
  dec_verify     隔日驗證結果（命中率累積）
  sector_daily   族群每日象限
  liv_record     李佛摩六欄逐日紀錄
  events         盤中變化事件流
"""
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import config as C

TW_TZ = timezone(timedelta(hours=8))
_lock = threading.RLock()


def today():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


def now_iso():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _conn():
    os.makedirs(os.path.dirname(C.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(C.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init():
    """建表。冪等，可重複呼叫。"""
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS stock_state(
          code TEXT PRIMARY KEY,
          track TEXT, state TEXT,
          last_price REAL, dist_ma20 REAL,
          aflow REAL, aflow_src TEXT, quad TEXT,
          health INTEGER, pattern TEXT,
          data_stage TEXT DEFAULT 'intraday_est',
          updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS dec_health(
          trade_date TEXT, code TEXT, name TEXT, sector TEXT, track TEXT,
          quad TEXT, score INTEGER, grade TEXT, stars INTEGER,
          chg REAL, ratio REAL, ratio_src TEXT, vr REAL,
          streak INTEGER, trend TEXT,
          chip_ok INTEGER, chip_note TEXT,
          inst_net_20d INTEGER, inst_streak INTEGER, big_holder_trend REAL,
          foreign_lots INTEGER, invest_lots INTEGER,
          close REAL, prev_close REAL, high REAL, low REAL, ma20 REAL,
          above_ma20 INTEGER, trigger REAL,
          sector_chg REAL, market_chg REAL, vs_sector REAL, vs_market REAL, rs TEXT,
          data_stage TEXT DEFAULT 'eod_final',
          updated_at TEXT,
          PRIMARY KEY(trade_date, code)
        );

        CREATE TABLE IF NOT EXISTS health_daily(
          trade_date TEXT, code TEXT,
          quadrant TEXT, health_score INTEGER,
          flow_streak INTEGER, health_trend TEXT,
          PRIMARY KEY(trade_date, code)
        );
        CREATE INDEX IF NOT EXISTS idx_health_daily_code ON health_daily(code, trade_date);

        CREATE TABLE IF NOT EXISTS dec_watchlist(
          obs_date TEXT, target_date TEXT, code TEXT, name TEXT, sector TEXT,
          track TEXT, grade TEXT, score INTEGER, quad TEXT,
          close REAL, chg REAL, vr REAL,
          foreign_lots INTEGER, invest_lots INTEGER, big_holder TEXT,
          obs_high REAL, reason TEXT,
          PRIMARY KEY(target_date, code)
        );

        CREATE TABLE IF NOT EXISTS dec_verify(
          target_date TEXT, code TEXT, grade TEXT, track TEXT,
          triggered INTEGER, entered INTEGER, success INTEGER,
          next_high_pct REAL, next_close_pct REAL,
          hold_days INTEGER, hold_ret_pct REAL,
          PRIMARY KEY(target_date, code)
        );

        CREATE TABLE IF NOT EXISTS sector_daily(
          trade_date TEXT, sector TEXT, pct REAL, amount_share REAL,
          flow_dir INTEGER, quadrant TEXT,
          PRIMARY KEY(trade_date, sector)
        );

        CREATE TABLE IF NOT EXISTS liv_record(
          trade_date TEXT, code TEXT, state TEXT, price REAL, pivot INTEGER,
          PRIMARY KEY(trade_date, code)
        );
        CREATE INDEX IF NOT EXISTS idx_liv_code ON liv_record(code, trade_date);

        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          trade_date TEXT, ts TEXT, code TEXT, kind TEXT, message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_date ON events(trade_date);

        CREATE TABLE IF NOT EXISTS inst_daily(
          trade_date TEXT, code TEXT, name TEXT,
          foreign_lots INTEGER, invest_lots INTEGER, dealer_lots INTEGER, total_lots INTEGER,
          source TEXT, fetched_at TEXT,
          PRIMARY KEY(trade_date, code)
        );
        CREATE INDEX IF NOT EXISTS idx_inst_daily_code ON inst_daily(code, trade_date);

        CREATE TABLE IF NOT EXISTS price_daily(
          trade_date TEXT, code TEXT, name TEXT,
          close REAL, open REAL, high REAL, low REAL, volume INTEGER,
          prev_close REAL, change_pct REAL, market TEXT,
          source TEXT, fetched_at TEXT,
          PRIMARY KEY(trade_date, code)
        );
        CREATE INDEX IF NOT EXISTS idx_price_daily_code ON price_daily(code, trade_date);
        """)
        existing = {r["name"] for r in c.execute("PRAGMA table_info(dec_health)").fetchall()}
        for name in ("inst_5d_net", "trust_5d_net", "margin_5d_chg"):
            if name not in existing:
                c.execute(f"ALTER TABLE dec_health ADD COLUMN {name} INTEGER")


# ── stock_state（盤中狀態機，崩潰復原用）──
def upsert_stock_state(row):
    with _lock, _conn() as c:
        c.execute("""INSERT OR REPLACE INTO stock_state
          (code,track,state,last_price,dist_ma20,aflow,aflow_src,quad,health,pattern,data_stage,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
          (row["code"], row.get("track"), row.get("state"), row.get("last_price"),
           row.get("dist_ma20"), row.get("aflow"), row.get("aflow_src"), row.get("quad"),
           row.get("health"), row.get("pattern"), row.get("data_stage", "intraday_est"),
           now_iso()))


def load_stock_states():
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM stock_state").fetchall()]


# ── dec_health（盤後蓋章）──
def save_dec_health(trade_date, rows):
    with _lock, _conn() as c:
        for r in rows:
            c.execute("""INSERT OR REPLACE INTO dec_health
              (trade_date,code,name,sector,track,quad,score,grade,stars,chg,ratio,ratio_src,vr,
               streak,trend,chip_ok,chip_note,inst_net_20d,inst_streak,big_holder_trend,
               foreign_lots,invest_lots,close,prev_close,high,low,ma20,above_ma20,trigger,
               sector_chg,market_chg,vs_sector,vs_market,rs,data_stage,updated_at,
               inst_5d_net,trust_5d_net,margin_5d_chg)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (trade_date, r["code"], r["name"], r["sector"], r["track"], r["quad"], r["score"],
               r["grade"], r["stars"], r["chg"], r["ratio"], r["ratio_src"], r["vr"], r["streak"],
               r["trend"], r["chip_ok"], r["chip_note"], r["inst_net_20d"], r["inst_streak"],
               r["big_holder_trend"], r["foreign_lots"], r["invest_lots"], r["close"],
               r["prev_close"], r["high"], r["low"], r["ma20"], 1 if r["above_ma20"] else 0,
               r["trigger"], r["sector_chg"], r["market_chg"], r["vs_sector"], r["vs_market"],
               r["rs"], "eod_final", now_iso(), r.get("inst_5d_net"), r.get("trust_5d_net"),
               r.get("margin_5d_chg", 0)))


def load_dec_health(trade_date=None):
    with _lock, _conn() as c:
        if trade_date is None:
            r = c.execute("SELECT MAX(trade_date) d FROM dec_health").fetchone()
            trade_date = r["d"]
        if not trade_date:
            return []
        return [dict(x) for x in c.execute(
            "SELECT * FROM dec_health WHERE trade_date=? ORDER BY score DESC",
            (trade_date,)).fetchall()]


def history_dates(limit=30):
    with _lock, _conn() as c:
        return [r["trade_date"] for r in c.execute(
            "SELECT DISTINCT trade_date FROM dec_health ORDER BY trade_date DESC LIMIT ?", (limit,)
        ).fetchall()]


def stock_history(code, limit=5, through=None):
    with _lock, _conn() as c:
        if through:
            rows = c.execute(
                "SELECT * FROM dec_health WHERE code=? AND trade_date<=? "
                "ORDER BY trade_date DESC LIMIT ?",
                (code, through, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM dec_health WHERE code=? ORDER BY trade_date DESC LIMIT ?",
                (code, limit),
            ).fetchall()
        return [dict(r) for r in rows][::-1]


def prev_dec_health(code, before_date):
    with _lock, _conn() as c:
        r = c.execute("""SELECT * FROM dec_health WHERE code=? AND trade_date<?
                         ORDER BY trade_date DESC LIMIT 1""",
                      (code, before_date)).fetchone()
        return dict(r) if r else None


# ── health_daily（時間序列）──
def save_health_daily(trade_date, code, quad, score, streak, trend):
    with _lock, _conn() as c:
        c.execute("""INSERT OR REPLACE INTO health_daily VALUES(?,?,?,?,?,?)""",
                  (trade_date, code, quad, score, streak, trend))


def prev_health_daily(code, before_date):
    with _lock, _conn() as c:
        r = c.execute("""SELECT * FROM health_daily WHERE code=? AND trade_date<?
                         ORDER BY trade_date DESC LIMIT 1""",
                      (code, before_date)).fetchone()
        return dict(r) if r else None


def quad_history(code, days=5):
    with _lock, _conn() as c:
        rows = c.execute("""SELECT trade_date, quadrant, health_score FROM health_daily
                            WHERE code=? ORDER BY trade_date DESC LIMIT ?""",
                         (code, days)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ── watchlist / verify ──
def save_watchlist(obs_date, target_date, rows):
    with _lock, _conn() as c:
        for r in rows:
            c.execute("""INSERT OR REPLACE INTO dec_watchlist VALUES
              (?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?, ?,?)""",
              (obs_date, target_date, r["code"], r["name"], r["sector"],
               r["track"], r["grade"], r["score"], r["quad"],
               r.get("close"), r.get("chg"), r.get("vr"),
               r.get("foreign_lots"), r.get("invest_lots"), r.get("big_holder"),
               r.get("obs_high"), r.get("reason", "")))


def load_watchlist(target_date):
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM dec_watchlist WHERE target_date=?", (target_date,)).fetchall()]


def save_verify(rows):
    with _lock, _conn() as c:
        for r in rows:
            c.execute("""INSERT OR REPLACE INTO dec_verify VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              (r["target_date"], r["code"], r["grade"], r["track"],
               r["triggered"], r["entered"], r["success"],
               r["next_high_pct"], r["next_close_pct"], r["hold_days"], r["hold_ret_pct"]))


# ── sector_daily ──
def save_sector_daily(trade_date, rows):
    with _lock, _conn() as c:
        for r in rows:
            c.execute("""INSERT OR REPLACE INTO sector_daily VALUES(?,?,?,?,?,?)""",
              (trade_date, r["sector"], r["pct"], r["amount_share"],
               r["flow_dir"], r["quadrant"]))


def sector_history(sector, days=5):
    with _lock, _conn() as c:
        rows = c.execute("""SELECT * FROM sector_daily WHERE sector=?
                            ORDER BY trade_date DESC LIMIT ?""",
                         (sector, days)).fetchall()
    return [dict(r) for r in reversed(rows)]


def prev_amount_share(sector):
    with _lock, _conn() as c:
        r = c.execute("""SELECT amount_share FROM sector_daily WHERE sector=?
                         ORDER BY trade_date DESC LIMIT 1""", (sector,)).fetchone()
        return r["amount_share"] if r else None


# ── 李佛摩 ──
def save_liv(trade_date, code, state, price, pivot):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO liv_record VALUES(?,?,?,?,?)",
                  (trade_date, code, state, price, 1 if pivot else 0))


def liv_history(code, days=20):
    with _lock, _conn() as c:
        rows = c.execute("""SELECT * FROM liv_record WHERE code=?
                            ORDER BY trade_date DESC LIMIT ?""", (code, days)).fetchall()
    return [dict(r) for r in reversed(rows)]


def liv_snapshot(trade_date=None):
    """回傳指定盤後日的李佛摩六欄快照。"""
    trade_date = trade_date or today()
    with _lock, _conn() as c:
        rows = c.execute(
            """SELECT l.*, COALESCE(d.name, '') AS name
               FROM liv_record l
               LEFT JOIN dec_health d ON d.trade_date=l.trade_date AND d.code=l.code
              WHERE l.trade_date=? ORDER BY l.code""",
            (trade_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def liv_history_through(code, through=None, days=20):
    """回傳截至指定盤後日的李佛摩逐日紀錄。"""
    with _lock, _conn() as c:
        if through:
            rows = c.execute(
                """SELECT * FROM liv_record
                   WHERE code=? AND trade_date<=?
                   ORDER BY trade_date DESC LIMIT ?""",
                (code, through, days),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT * FROM liv_record WHERE code=?
                   ORDER BY trade_date DESC LIMIT ?""",
                (code, days),
            ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ── events（盤中變化事件流）──
def add_event(code, kind, message):
    with _lock, _conn() as c:
        c.execute("INSERT INTO events(trade_date,ts,code,kind,message) VALUES(?,?,?,?,?)",
                  (today(), now_iso(), code, kind, message))


def load_events(trade_date=None, limit=50):
    trade_date = trade_date or today()
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM events WHERE trade_date=? ORDER BY id DESC LIMIT ?",
            (trade_date, limit)).fetchall()]


# ── inst_daily（當日三大法人持久化）──
def save_inst_daily(trade_date, rows, source="twse_t86"):
    with _lock, _conn() as c:
        for r in rows:
            c.execute("""INSERT OR REPLACE INTO inst_daily VALUES
              (?,?,?,?,?,?,?,?,?)""",
              (trade_date, r["code"], r.get("name", ""),
               r.get("foreign_lots", 0), r.get("invest_lots", 0),
               r.get("dealer_lots", 0), r.get("total_lots", 0),
               source, now_iso()))


def load_inst_daily(trade_date, code=None):
    with _lock, _conn() as c:
        if code:
            r = c.execute("""SELECT * FROM inst_daily
                             WHERE trade_date=? AND code=?""",
                          (trade_date, code)).fetchone()
            return dict(r) if r else None
        return [dict(r) for r in c.execute(
            "SELECT * FROM inst_daily WHERE trade_date=? ORDER BY code",
            (trade_date,)).fetchall()]


def load_inst_recent(code, days=5):
    """回傳個股近 N 日法人（舊→新）"""
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT * FROM inst_daily WHERE code=?
               ORDER BY trade_date DESC LIMIT ?""",
            (code, days))]


# ── price_daily（個股日收盤價持久化）──
def save_price_daily(trade_date, rows, market, source):
    with _lock, _conn() as c:
        for r in rows:
            c.execute("""INSERT OR REPLACE INTO price_daily VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (trade_date, r["code"], r.get("name", ""),
               r.get("close"), r.get("open"), r.get("high"), r.get("low"),
               r.get("volume", 0), r.get("prev_close"), r.get("change_pct"),
               market, source, now_iso()))


def load_price_daily(trade_date, code=None, market=None):
    with _lock, _conn() as c:
        sql = "SELECT * FROM price_daily WHERE trade_date=?"
        params = [trade_date]
        if code:
            sql += " AND code=?"
            params.append(code)
        if market:
            sql += " AND market=?"
            params.append(market)
        sql += " ORDER BY code"
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def load_price_recent(code, days=30):
    """回傳個股近 N 日收盤（舊→新）"""
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT * FROM price_daily WHERE code=?
               ORDER BY trade_date DESC LIMIT ?""",
            (code, days))]
