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


def _column_exists(c, table, column):
    return any(r["name"] == column
               for r in c.execute(f"PRAGMA table_info({table})"))


def _add_column(c, table, column, decl):
    """冪等遷移：欄位不存在才 ADD COLUMN，避免重啟時 migration 重複執行報錯。"""
    if not _column_exists(c, table, column):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
        CREATE TABLE IF NOT EXISTS watch_outcome(
          trade_date TEXT, stock_id TEXT, stock_name TEXT, sector TEXT,
          watch_reason TEXT,              -- 昨晚入選理由
          open_group TEXT,                -- 盤中首次分類
          close_group TEXT,               -- 收盤時分類
          close_price REAL, change_rate REAL, aflow REAL, volume_ratio REAL,
          verdict TEXT,                   -- 命中 / 未命中 / 反向
          note TEXT, stamped_at TEXT,
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
        CREATE TABLE IF NOT EXISTS watch_reject(
          trade_date  TEXT NOT NULL,     -- 供隔日使用的名單日
          stock_id    TEXT NOT NULL,
          stock_name  TEXT,
          sector      TEXT,
          source      TEXT NOT NULL,     -- radar / resilient（同檔可雙流程各落選一次）
          factor_score REAL,             -- 七因子總分（radar 來源；相容欄，等同 score_total）
          fail_factor TEXT,              -- 卡在哪：'量比<0.8' / '法人買超<=0' / '七因子<65' ...
          detail      TEXT,              -- 該因子實際值，例如 '量比0.6'
          model_version TEXT,
          created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(trade_date, stock_id, source)
        );
        CREATE INDEX IF NOT EXISTS idx_sig_date ON signals(trade_date, stock_id);
        """)
        # ── 遷移:一律走 PRAGMA 檢查後再 ADD COLUMN ────────────────
        # SQLite 的 ADD COLUMN 無可靠 IF NOT EXISTS，服務每次重啟都會跑
        # init()，直接 ALTER 會在第二次啟動噴 "duplicate column"。
        _add_column(c, "signals", "factors", "TEXT")
        # watchlist：名單來源 / 進場基準 / 因子分數 / 選入分類 / 模型版本
        _add_column(c, "watchlist", "source", "TEXT")
        _add_column(c, "watchlist", "entry_ref", "REAL")
        _add_column(c, "watchlist", "factor_score", "REAL")
        _add_column(c, "watchlist", "group_at_pick", "TEXT")
        _add_column(c, "watchlist", "model_version", "TEXT")
        # watchlist：昨日訊號型態 + 明日觸發價（六型態分類，選股當下算並存，
        # 供 B 卡盤後驗證顯示「昨日訊號型態」與「原定進場：突破昨高 X」）。
        _add_column(c, "watchlist", "signal_type", "TEXT")
        _add_column(c, "watchlist", "trigger_price", "REAL")
        # watch_outcome：今日觸發判定 + 未觸發的【明確原因】（T+1 蓋章當下算，
        # 取代前端「原定進場條件未成立」廣泛語）。
        _add_column(c, "watch_outcome", "signal_type", "TEXT")
        _add_column(c, "watch_outcome", "trigger_status", "TEXT")
        _add_column(c, "watch_outcome", "trigger_price", "REAL")
        _add_column(c, "watch_outcome", "non_trigger_reason", "TEXT")
        _add_column(c, "watch_outcome", "entry_ref", "REAL")
        _add_column(c, "watch_outcome", "today_high", "REAL")
        _add_column(c, "watch_outcome", "intraday_breakout", "INTEGER")
        _add_column(c, "watch_outcome", "close_confirmed", "INTEGER")
        # watch_reject：逐因子分數（radar 路徑回填）。radar 七因子 taxonomy 為
        # money_health/absorption/net_active/vs_ma20/inst_streak/margin，與下列
        # 通用欄非一一對應；只把「真正對得上」的填入具名欄（volume=net_active、
        # rs=vs_ma20、chip=inst_streak），完整逐因子 points 另存 factors_json，
        # 供 Phase 5 離線分析。其餘欄留 NULL 不硬塞（避免標錯）。
        for _c in ("score_trend", "score_volume", "score_chip",
                   "score_sector", "score_rs", "score_ai", "score_total"):
            _add_column(c, "watch_reject", _c, "REAL")
        _add_column(c, "watch_reject", "factors_json", "TEXT")
        # review_log：報酬分布統計（績效歸因基石）+ 模型版本
        _add_column(c, "review_log", "avg_return", "REAL")
        _add_column(c, "review_log", "median_return", "REAL")
        _add_column(c, "review_log", "max_return", "REAL")
        _add_column(c, "review_log", "min_return", "REAL")
        _add_column(c, "review_log", "avg_holding_days", "REAL")
        _add_column(c, "review_log", "model_version", "TEXT")


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
    """存明日觀察名單。新欄位（source/entry_ref/factor_score/group_at_pick/
    model_version）為選填，舊呼叫端不帶時寫 NULL / 預設版本，向後相容。"""
    import config as _C
    with _lock, _conn() as c:
        for r in rows:
            c.execute("""INSERT OR REPLACE INTO watchlist
              (trade_date,stock_id,stock_name,sector,reason,
               source,entry_ref,factor_score,group_at_pick,model_version,
               signal_type,trigger_price)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
              (trade_date, r["code"], r["name"], r["sector"], r["reason"],
               r.get("source"), r.get("entry_ref"), r.get("factor_score"),
               r.get("group_at_pick"),
               r.get("model_version", getattr(_C, "MODEL_VERSION", None)),
               r.get("signal_type"), r.get("trigger_price")))


def save_watch_rejects(trade_date, rows):
    """存落選池：同一名單日、同一 source 只留一筆（PK 含 source）。
    rows 需含 code/source/fail_factor；score_* 與 detail 為選填，
    Phase 1 抗跌路徑只帶得出 fail_factor/detail，七因子分數留 NULL，
    待 Phase 3/4 由 radar 路徑回填。"""
    import config as _C
    ver = getattr(_C, "MODEL_VERSION", None)
    with _lock, _conn() as c:
        for r in rows:
            c.execute("""INSERT OR REPLACE INTO watch_reject
              (trade_date,stock_id,stock_name,sector,source,factor_score,
               fail_factor,detail,model_version,
               score_trend,score_volume,score_chip,score_sector,
               score_rs,score_ai,score_total,factors_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (trade_date, r["code"], r.get("name"), r.get("sector"),
               r["source"], r.get("factor_score"), r.get("fail_factor"),
               r.get("detail"), r.get("model_version", ver),
               r.get("score_trend"), r.get("score_volume"), r.get("score_chip"),
               r.get("score_sector"), r.get("score_rs"), r.get("score_ai"),
               r.get("score_total"), r.get("factors_json")))


def load_watch_rejects(trade_date):
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM watch_reject WHERE trade_date=? ORDER BY source, stock_id",
            (trade_date,))]


def save_watch_outcome(trade_date, rows):
    """收盤蓋章：把今日盯盤名單的實際結果寫入歷史，供準確度回測。"""
    with _lock, _conn() as c:
        for r in rows:
            c.execute("""INSERT OR REPLACE INTO watch_outcome
              (trade_date,stock_id,stock_name,sector,watch_reason,open_group,
               close_group,close_price,change_rate,aflow,volume_ratio,
               verdict,note,stamped_at,
               signal_type,trigger_status,trigger_price,non_trigger_reason,
               entry_ref,today_high,intraday_breakout,close_confirmed)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (trade_date, r.get("code"), r.get("name"), r.get("sector"),
               r.get("watch_reason"), r.get("open_group"), r.get("close_group"),
               r.get("close_price"), r.get("change_rate"), r.get("aflow"),
               r.get("volume_ratio"), r.get("verdict"), r.get("note"), now_iso(),
               r.get("signal_type"), r.get("trigger_status"),
               r.get("trigger_price"), r.get("non_trigger_reason"),
               r.get("entry_ref"), r.get("today_high"),
               1 if r.get("intraday_breakout") else 0 if r.get("intraday_breakout") is not None else None,
               1 if r.get("close_confirmed") else 0 if r.get("close_confirmed") is not None else None))


def load_watch_outcome(trade_date=None, limit_days=30):
    with _lock, _conn() as c:
        if trade_date:
            return [dict(r) for r in c.execute(
                "SELECT * FROM watch_outcome WHERE trade_date=? ORDER BY stock_id",
                (trade_date,))]
        return [dict(r) for r in c.execute(
            """SELECT * FROM watch_outcome WHERE trade_date IN
               (SELECT DISTINCT trade_date FROM watch_outcome
                ORDER BY trade_date DESC LIMIT ?)
               ORDER BY trade_date DESC, stock_id""", (limit_days,))]


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
def write_review(trade_date, total, hit, missed, notes="", stats=None):
    """收盤驗證逐日彙總。stats（選填）帶報酬分布：
    {avg_return, median_return, max_return, min_return, avg_holding_days}。
    改用具名欄位 INSERT（不再靠欄位順序），相容遷移後新增的統計欄。"""
    import config as _C
    rate = round(hit / total * 100, 1) if total else 0.0
    stats = stats or {}
    with _lock, _conn() as c:
        c.execute("""INSERT OR REPLACE INTO review_log
          (trade_date,watch_total,watch_hit,hit_rate,missed_stocks,notes,
           avg_return,median_return,max_return,min_return,avg_holding_days,
           model_version)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
          (trade_date, total, hit, rate,
           json.dumps(missed, ensure_ascii=False), notes,
           stats.get("avg_return"), stats.get("median_return"),
           stats.get("max_return"), stats.get("min_return"),
           stats.get("avg_holding_days"),
           stats.get("model_version", getattr(_C, "MODEL_VERSION", None))))
    return rate


def review_dates(limit=60):
    """有驗證資料的交易日（新→舊）；給盤後驗證頁的日期選擇器。"""
    with _lock, _conn() as c:
        return [r["trade_date"] for r in c.execute(
            "SELECT trade_date FROM review_log ORDER BY trade_date DESC LIMIT ?",
            (limit,))]


def latest_review_date():
    """最近一個有驗證資料的交易日；UI 預設落此日，避免週末/隔日開啟全空白。"""
    with _lock, _conn() as c:
        r = c.execute(
            "SELECT MAX(trade_date) d FROM review_log").fetchone()
        return r["d"] if r and r["d"] else None


def latest_review_date_with_data():
    """最近一個『真的有收盤資料』的驗證日：watch_outcome 至少一筆 close_price 非空。
    B 卡保底用——避免落在只有 review_log 空殼、outcomes 全無收盤的日子而顯示
    『尚未抓到資料』。無任何有資料日時回 None，呼叫端再退回 latest_review_date。"""
    with _lock, _conn() as c:
        r = c.execute(
            """SELECT MAX(trade_date) d FROM watch_outcome
               WHERE close_price IS NOT NULL""").fetchone()
        return r["d"] if r and r["d"] else None


def recent_hit_rates(days=30):
    """逐日命中率趨勢，含報酬分布（Phase 2 趨勢表用）。"""
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT trade_date, hit_rate, avg_return, median_return,
                      max_return, min_return, model_version
               FROM review_log ORDER BY trade_date DESC LIMIT ?""", (days,))]


def review_summary(trade_date):
    """某交易日的驗證彙總（review_log 單列）；無則回 None。"""
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM review_log WHERE trade_date=?",
                      (trade_date,)).fetchone()
        return dict(r) if r else None


def review_outcomes(trade_date):
    """某交易日逐檔 T+1 驗證：watch_outcome 併 watchlist 的 source/entry_ref/
    factor_score/group_at_pick。watch_outcome 尚無資料（改版前的舊日）時，
    退回 watchlist 逐檔並標 verdict=待驗證，讓歷史日仍可回看名單。"""
    with _lock, _conn() as c:
        wl = {r["stock_id"]: dict(r) for r in c.execute(
            "SELECT * FROM watchlist WHERE trade_date=?", (trade_date,))}
        oc = [dict(r) for r in c.execute(
            "SELECT * FROM watch_outcome WHERE trade_date=? ORDER BY stock_id",
            (trade_date,))]
    rows = []
    if oc:
        for o in oc:
            w = wl.get(o["stock_id"], {})
            rows.append({
                "code": o["stock_id"], "name": o.get("stock_name") or o["stock_id"],
                "sector": o.get("sector"), "source": w.get("source"),
                "entry_ref": w.get("entry_ref"), "factor_score": w.get("factor_score"),
                "group_at_pick": w.get("open_group") or o.get("open_group"),
                "close_group": o.get("close_group"), "close_price": o.get("close_price"),
                "change_rate": o.get("change_rate"), "verdict": o.get("verdict"),
                "note": o.get("note"), "watch_reason": o.get("watch_reason"),
                # 昨日訊號型態 + 今日觸發狀態 + 未觸發明確原因（B 卡直接讀）
                "signal_type": o.get("signal_type") or w.get("signal_type"),
                "trigger_price": o.get("trigger_price") or w.get("trigger_price"),
                "trigger_status": o.get("trigger_status"),
                "non_trigger_reason": o.get("non_trigger_reason"),
            })
    else:
        for sid, w in sorted(wl.items()):
            rows.append({
                "code": sid, "name": w.get("stock_name") or sid,
                "sector": w.get("sector"), "source": w.get("source"),
                "entry_ref": w.get("entry_ref"), "factor_score": w.get("factor_score"),
                "group_at_pick": w.get("group_at_pick"), "close_group": None,
                "close_price": None, "change_rate": None, "verdict": "待驗證",
                "note": None, "watch_reason": w.get("reason"),
                # 尚未蓋章：型態/觸發價已在選股當下存於 watchlist，先帶出；
                # 觸發狀態/原因要等 T+1 收盤才有。
                "signal_type": w.get("signal_type"),
                "trigger_price": w.get("trigger_price"),
                "trigger_status": None, "non_trigger_reason": None,
            })
    return rows


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


def prev_amount_share(sector):
    with _lock, _conn() as c:
        r = c.execute("""SELECT amount_share FROM sector_daily
          WHERE sector=? ORDER BY trade_date DESC LIMIT 1""", (sector,)).fetchone()
        return r["amount_share"] if r else None


def today_stats():
    with _lock, _conn() as c:
        sig = c.execute("""SELECT
            COUNT(*) raw_events,
            COUNT(DISTINCT stock_id || '|' || action) unique_signals,
            COUNT(DISTINCT stock_id) unique_stocks,
            COUNT(DISTINCT CASE WHEN action='buy' THEN stock_id END) unique_buy_stocks,
            COUNT(DISTINCT CASE WHEN action='sell' THEN stock_id END) unique_risk_stocks
          FROM signals WHERE trade_date=?""", (today(),)).fetchone()
        out = dict(sig)
        # 相容舊欄位，但改成去重數字，避免每輪掃描被誤算成新訊號。
        out["total"] = out["unique_signals"] or 0
        out["buys"] = out["unique_buy_stocks"] or 0
        out["risks"] = out["unique_risk_stocks"] or 0
        return out


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
