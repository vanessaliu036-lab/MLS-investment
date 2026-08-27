"""
store.py — 資料落地層(全系統唯一的 SQLite 出入口)

擋住的痛點:
1. 同一天同一檔只抓一次。抓完寫進 SQLite,之後任何模組都只讀 DB,不再打 API。
2. 已收盤日期的資料不可變。DB trigger 層面擋 UPDATE/DELETE,不靠程式自律。
3. 一張表只有一個 owner 插件。非 owner 寫入 → 直接 raise,不執行。
   新插件只能建自己的新表,永遠改不壞已驗證的盤後資料。
4. 多來源 fallback:TWSE 官方 → TPEx → FinMind。誰先回誰算數,不比對、不交叉驗證。
   差幾百張、一千張都不影響「法人站買方還是賣方」這個級距的判斷。
5. fetch_log 記錄每次真的打了哪個 API。修完後重開服務應該是零筆新紀錄。

讀 → 隨便讀,不設限,一百個插件同時讀同一張表都不會出事。
寫 → 一張表只有一個 owner,別人寫直接擋。
抓 → 只有 owner 能打 API,而且先查 DB 這天有沒有抓過。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterable

from phase import Phase, assert_can_read, get_phase, today_tw

DB_PATH = "mls.db"

# ---------------------------------------------------------------- 寫入權限鎖
#
# 每張表註冊唯一 owner。這是第一層防護,也是解掉八成痛點的那一條:
# 新插件就算寫了 UPDATE inst_flow,也會在執行前被擋掉。

TABLE_OWNER: dict[str, str] = {
    "pa_snapshot": "pre_activation",   # Pre-Activation 每日快照(2026-08-24 起)
    "inst_flow": "post_pipeline",      # 法人買賣超(死值)
    "margin": "post_pipeline",         # 融資融券(死值)
    "daily_bar": "post_pipeline",      # 日 K / MA / 均量(死值)
    "aflow": "intraday",               # 盤中主動買賣差(當日可變)
    "quote_snap": "intraday",          # 盤中即時價量(當日可變)
    "absorption": "absorption",        # 承接品質
    "money_health": "money_health",    # 資金健康度
    "watchlist_pre": "screen_pre",     # 保留:盤前名單=直接讀昨日盤後,不重算
    "watchlist_intraday": "screen_intraday",
    "watchlist_post": "screen_post",
    "candidate_pool": "screen_post",     # 隔日候選池:盤後產出,盤中只讀
    "dropped_pool": "screen_post",       # 當日被淘汰(真結構失效)名單,留痕供顯示/複盤
    "intraday_signal": "screen_intraday", # 盤中燈號
    # ---- B 鏈專屬表。owner 全歸 B 鏈,A 鏈永遠寫不進來。 ----
    "b_snapshot": "b_snapshot",       # 盤中時序快照(每5分鐘)
    "b_discovery": "b_discover",      # 13:20 掃描標記
    "b_verified": "b_verify",         # 盤後法人驗證結果
    # ---- 市場層級資料(TWSE/TPEx 官方,免費無上限) ----
    "market_breadth": "market",       # 指數、成交金額、漲跌家數
    "sector_index": "market",         # 類股指數與成交比重
    # ---- 漏斗(逐層淘汰) ----
    "funnel_result": "funnel",        # 每日各層存活名單
    "funnel_log": "funnel",           # 每層淘汰理由統計
    "reject_outcome": "reject_verify", # 排除名單 T+1 錯殺率量測
    "fetch_log": "store",
    "plugin_status": "store",
    "post_checksum": "store",
}

# 這些表存的是已收盤的死值 → 歷史日期永不可變
IMMUTABLE_TABLES = {"inst_flow", "margin", "daily_bar", "watchlist_post"}


class TableOwnershipError(RuntimeError):
    """非 owner 插件試圖寫入他人的表。插件之間禁止共用寫入路徑。"""


class ImmutableDataError(RuntimeError):
    """試圖修改已收盤日期的資料。已驗證的盤後值物理上改不動。"""


_local = threading.local()


@contextmanager
def conn(db_path: str = DB_PATH):
    if not hasattr(_local, "conn") or _local.path != db_path:
        c = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
        _local.path = db_path
    yield _local.conn


# ---------------------------------------------------------------- schema

_SCHEMA = """
-- 死值表:唯一鍵 (code, data_date),INSERT OR IGNORE,同一天同一檔只會有一筆
CREATE TABLE IF NOT EXISTS inst_flow (
    code TEXT NOT NULL, data_date TEXT NOT NULL,
    foreign_net INTEGER, trust_net INTEGER, dealer_net INTEGER, total_net INTEGER,
    consecutive_days INTEGER,
    foreign_days INTEGER, trust_days INTEGER, dealer_days INTEGER,
    source TEXT, fetched_at TEXT,
    PRIMARY KEY (code, data_date)
);

CREATE TABLE IF NOT EXISTS margin (
    code TEXT NOT NULL, data_date TEXT NOT NULL,
    margin_balance INTEGER, margin_change INTEGER,
    short_balance INTEGER, short_change INTEGER,
    source TEXT, fetched_at TEXT,
    PRIMARY KEY (code, data_date)
);

CREATE TABLE IF NOT EXISTS daily_bar (
    code TEXT NOT NULL, data_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    ma5 REAL, ma20 REAL, ma60 REAL, vol_ma20 INTEGER,
    source TEXT, fetched_at TEXT,
    PRIMARY KEY (code, data_date)
);

-- 盤中表:當日可更新,收盤後轉不可變
CREATE TABLE IF NOT EXISTS aflow (
    code TEXT NOT NULL, data_date TEXT NOT NULL,
    active_buy REAL, active_sell REAL, net_active REAL,
    method TEXT, updated_at TEXT,
    PRIMARY KEY (code, data_date, method)
);

CREATE TABLE IF NOT EXISTS quote_snap (
    code TEXT NOT NULL, data_date TEXT NOT NULL,
    price REAL, change_rate REAL, volume INTEGER,
    bid_vol INTEGER, ask_vol INTEGER,
    high REAL, low REAL, avg_price REAL, updated_at TEXT,
    PRIMARY KEY (code, data_date)
);

CREATE TABLE IF NOT EXISTS absorption (
    code TEXT NOT NULL, data_date TEXT NOT NULL,
    score REAL, grade TEXT, status TEXT, reason TEXT, updated_at TEXT,
    PRIMARY KEY (code, data_date)
);

CREATE TABLE IF NOT EXISTS money_health (
    code TEXT NOT NULL, data_date TEXT NOT NULL,
    score REAL, quadrant TEXT, status TEXT, reason TEXT, updated_at TEXT,
    PRIMARY KEY (code, data_date)
);

-- 名單表:每個時段一張,各自獨立,互不共用
CREATE TABLE IF NOT EXISTS watchlist_intraday (
    data_date TEXT NOT NULL, code TEXT NOT NULL,
    rank INTEGER, score REAL, payload TEXT, generated_at TEXT,
    PRIMARY KEY (data_date, code)
);

CREATE TABLE IF NOT EXISTS watchlist_post (
    data_date TEXT NOT NULL, code TEXT NOT NULL,
    rank INTEGER, score REAL, payload TEXT, generated_at TEXT,
    PRIMARY KEY (data_date, code)
);

-- 稽核:實際打了幾次 API。重開服務後查這張表應該是零筆新紀錄。
-- 隔日候選池:盤後寬篩產出,隔天盤中只盯這張表
CREATE TABLE IF NOT EXISTS candidate_pool (
    data_date TEXT NOT NULL, code TEXT NOT NULL,
    rank INTEGER, score REAL, track TEXT,
    trigger_price REAL, entry_rule TEXT,
    payload TEXT, generated_at TEXT,
    PRIMARY KEY (data_date, code)
);

-- 被淘汰名單(真結構失效,tier=淘汰):留痕供淘汰名單顯示與 T+1 複盤,不刪除。
CREATE TABLE IF NOT EXISTS dropped_pool (
    data_date TEXT NOT NULL, code TEXT NOT NULL,
    payload TEXT, generated_at TEXT,
    PRIMARY KEY (data_date, code)
);

-- 盤中燈號:嚴判結果
CREATE TABLE IF NOT EXISTS intraday_signal (
    data_date TEXT NOT NULL, code TEXT NOT NULL,
    light TEXT, conditions TEXT, note TEXT, updated_at TEXT,
    PRIMARY KEY (data_date, code)
);

-- ============ B 鏈專屬表(獨立於 A 鏈,不共用任何表) ============

-- 盤中時序快照:每 5 分鐘從記憶體 buffer 寫一筆。零 API 呼叫。
CREATE TABLE IF NOT EXISTS b_snapshot (
    data_date TEXT NOT NULL, code TEXT NOT NULL, slot TEXT NOT NULL,
    price REAL, change_rate REAL, volume INTEGER,
    net_active REAL, bid_vol INTEGER, ask_vol INTEGER,
    created_at TEXT,
    -- 2026-08-26:source freshness 純觀察欄位,不參與任何判斷/scoring。
    -- quote_snap/aflow 合併時原本不比對 updated_at,可能拼到「新價舊 aflow」;
    -- 這三欄只記事實(來源各自的寫入時間、兩者相差幾秒、aflow 這輪走哪條管線),
    -- 不設 stale 門檻、不回填 None——要等資料累積夠了才回頭研究多少秒算 stale。
    quote_updated_at TEXT, aflow_updated_at TEXT, freshness_gap_sec REAL,
    aflow_method TEXT,
    PRIMARY KEY (data_date, code, slot)
);
CREATE INDEX IF NOT EXISTS idx_b_snap ON b_snapshot(data_date, code, slot);

-- 13:20 最終掃描的標記結果
CREATE TABLE IF NOT EXISTS b_discovery (
    data_date TEXT NOT NULL, code TEXT NOT NULL,
    hits INTEGER, criteria TEXT, detail TEXT, scanned_at TEXT,
    PRIMARY KEY (data_date, code)
);

-- 盤後法人驗證結果
CREATE TABLE IF NOT EXISTS b_verified (
    data_date TEXT NOT NULL, code TEXT NOT NULL,
    verdict TEXT, inst_net INTEGER, reason TEXT, verified_at TEXT,
    PRIMARY KEY (data_date, code)
);

-- ============ 市場層級(TWSE/TPEx 官方) ============
-- 真正的市場寬度 = 上漲家數 / 總家數。不是 aflow 正值占比。
CREATE TABLE IF NOT EXISTS market_breadth (
    data_date TEXT NOT NULL, market TEXT NOT NULL, slot TEXT NOT NULL,
    index_value REAL, index_change REAL, index_change_pct REAL,
    turnover REAL,
    advancing INTEGER, declining INTEGER, unchanged INTEGER,
    limit_up INTEGER, limit_down INTEGER,
    source TEXT, created_at TEXT,
    PRIMARY KEY (data_date, market, slot)
);

-- 類股指數:判斷是全面性下跌還是單一族群被壓
CREATE TABLE IF NOT EXISTS sector_index (
    data_date TEXT NOT NULL, market TEXT NOT NULL,
    sector TEXT NOT NULL, slot TEXT NOT NULL,
    value REAL, change_pct REAL, turnover REAL, turnover_share REAL,
    source TEXT, created_at TEXT,
    PRIMARY KEY (data_date, market, sector, slot)
);

-- ============ 漏斗:逐層淘汰(不是排序取前 N) ============
-- 每一層都是通過/淘汰的判斷。留下幾檔就是幾檔,可能 0 檔。
CREATE TABLE IF NOT EXISTS funnel_result (
    data_date TEXT NOT NULL, layer TEXT NOT NULL, code TEXT NOT NULL,
    survived INTEGER,          -- 1=通過 0=淘汰
    reasons TEXT,              -- 淘汰或通過的理由
    detail TEXT,
    decided_at TEXT,           -- 這一層定案的時間
    PRIMARY KEY (data_date, layer, code)
);

-- 每層淘汰理由分布。兩週後靠這張表決定放鬆哪一條門檻。
CREATE TABLE IF NOT EXISTS funnel_log (
    data_date TEXT NOT NULL, layer TEXT NOT NULL,
    entered INTEGER, survived INTEGER, dropped INTEGER,
    reason_breakdown TEXT,     -- {"量能不足":12,"破月線":6}
    decided_at TEXT,
    PRIMARY KEY (data_date, layer)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT, code TEXT, data_date TEXT,
    source TEXT, ok INTEGER, note TEXT, fetched_at TEXT
);

-- 插件執行狀態(信封落地)
CREATE TABLE IF NOT EXISTS plugin_status (
    plugin TEXT NOT NULL, data_date TEXT NOT NULL, phase TEXT,
    status TEXT, reason TEXT, updated_at TEXT,
    PRIMARY KEY (plugin, data_date, phase)
);

-- 已驗證盤後值的指紋。加插件之後比對,被動到就報錯。
CREATE TABLE IF NOT EXISTS post_checksum (
    data_date TEXT PRIMARY KEY,
    checksum TEXT, row_count INTEGER, created_at TEXT
);
"""

# DB 層面的不可變 trigger — 不靠程式自律
_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS inst_flow_no_update BEFORE UPDATE ON inst_flow
WHEN OLD.data_date < date('now','localtime')
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: inst_flow 歷史資料不可修改'); END;

CREATE TRIGGER IF NOT EXISTS inst_flow_no_delete BEFORE DELETE ON inst_flow
WHEN OLD.data_date < date('now','localtime')
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: inst_flow 歷史資料不可刪除'); END;

CREATE TRIGGER IF NOT EXISTS margin_no_update BEFORE UPDATE ON margin
WHEN OLD.data_date < date('now','localtime')
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: margin 歷史資料不可修改'); END;

CREATE TRIGGER IF NOT EXISTS margin_no_delete BEFORE DELETE ON margin
WHEN OLD.data_date < date('now','localtime')
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: margin 歷史資料不可刪除'); END;

CREATE TRIGGER IF NOT EXISTS daily_bar_no_update BEFORE UPDATE ON daily_bar
WHEN OLD.data_date < date('now','localtime')
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: daily_bar 歷史資料不可修改'); END;

CREATE TRIGGER IF NOT EXISTS daily_bar_no_delete BEFORE DELETE ON daily_bar
WHEN OLD.data_date < date('now','localtime')
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: daily_bar 歷史資料不可刪除'); END;

-- 盤中資料收盤後轉不可變(而不是被清空)
CREATE TRIGGER IF NOT EXISTS aflow_freeze BEFORE UPDATE ON aflow
WHEN OLD.data_date < date('now','localtime')
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: aflow 過往交易日已凍結,不可修改'); END;

CREATE TRIGGER IF NOT EXISTS quote_snap_freeze BEFORE UPDATE ON quote_snap
WHEN OLD.data_date < date('now','localtime')
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE: quote_snap 過往交易日已凍結,不可修改'); END;
"""


# 既有 DB 的欄位補丁：CREATE TABLE IF NOT EXISTS 不會替「已存在的表」加新欄,
# 故 schema 新增欄位時,舊 DB 需在此明列 ADD COLUMN(冪等:已存在就跳過)。
# ADD COLUMN 不觸發 immutable 的 BEFORE UPDATE/DELETE trigger,對死值表安全。
_COLUMN_MIGRATIONS = {
    "inst_flow": [("foreign_days", "INTEGER"), ("trust_days", "INTEGER"),
                  ("dealer_days", "INTEGER")],
    # 2026-08-18:A 卡補「距買點/日內位置/VWAP乖離」判讀,quote_snap 舊表要補欄位。
    "quote_snap": [("high", "REAL"), ("low", "REAL"), ("avg_price", "REAL")],
    # 2026-08-26:b_snapshot 補 source freshness 觀察欄位,見上方 b_snapshot 建表註解。
    "b_snapshot": [("quote_updated_at", "TEXT"), ("aflow_updated_at", "TEXT"),
                   ("freshness_gap_sec", "REAL"), ("aflow_method", "TEXT")],
}


def _migrate_columns(c) -> None:
    for table, cols in _COLUMN_MIGRATIONS.items():
        try:
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        except Exception:
            continue
        if not have:            # 表還不存在(將由 _SCHEMA 建成最新版),不需補
            continue
        for name, typ in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def init_db(db_path: str = DB_PATH) -> None:
    with conn(db_path) as c:
        c.executescript(_SCHEMA)
        c.executescript(_TRIGGERS)
        _migrate_columns(c)
        c.commit()


# ---------------------------------------------------------------- 權限守門

def _check_owner(table: str, plugin: str) -> None:
    owner = TABLE_OWNER.get(table)
    if owner is None:
        # 未註冊的表 = 新插件自己建的表,允許,但必須註冊後才受保護
        return
    if owner != plugin:
        raise TableOwnershipError(
            f"插件 '{plugin}' 不得寫入表 '{table}'(owner='{owner}')。"
            f"插件之間禁止共用寫入路徑:讀可以共用,寫絕對不行。"
            f"若需要新欄位,請建立你自己的新表。"
        )


def _check_immutable(table: str, code: str, data_date: _dt.date | str,
                     db_path: str = DB_PATH) -> None:
    """
    只擋「覆蓋既有的死值」,不擋第一次寫入。
    第一次一定要抓、要寫,不然 DB 是空的。同一天只抓一次講的是第二次以後不再打 API。
    """
    if table not in IMMUTABLE_TABLES:
        return
    d = data_date if isinstance(data_date, str) else data_date.isoformat()
    if _dt.date.fromisoformat(d) >= today_tw():
        return  # 今日,還在變,允許
    if read_one(table, code, d, db_path) is not None:
        raise ImmutableDataError(
            f"表 '{table}' 的 ({code}, {d}) 已存在且已收盤,不可覆蓋。"
            f"已收盤的日期,資料抓過一次就永久不變。"
        )


def register_table(table: str, plugin: str) -> None:
    """新插件建自己的表之後呼叫這支登記 owner,之後別人就動不了。"""
    existing = TABLE_OWNER.get(table)
    if existing and existing != plugin:
        raise TableOwnershipError(f"表 '{table}' 已由 '{existing}' 擁有,不可轉移。")
    TABLE_OWNER[table] = plugin


# ---------------------------------------------------------------- 讀(不設限)

def read_one(table: str, code: str, data_date: _dt.date | str,
             db_path: str = DB_PATH) -> dict | None:
    d = data_date.isoformat() if isinstance(data_date, _dt.date) else data_date
    with conn(db_path) as c:
        row = c.execute(
            f"SELECT * FROM {table} WHERE code=? AND data_date=?", (code, d)
        ).fetchone()
    return dict(row) if row else None


def read_date(table: str, data_date: _dt.date | str,
              db_path: str = DB_PATH) -> dict[str, dict]:
    """讀某一天全部標的。回傳 {code: row}。缺的檔不會出現在 dict 裡。"""
    d = data_date.isoformat() if isinstance(data_date, _dt.date) else data_date
    with conn(db_path) as c:
        rows = c.execute(f"SELECT * FROM {table} WHERE data_date=?", (d,)).fetchall()
    return {r["code"]: dict(r) for r in rows}


def read_recent(table: str, code: str, upto: _dt.date | str, n: int,
                db_path: str = DB_PATH) -> list[dict]:
    """讀單一 code 截至 upto(含)最近 n 個交易日,由新到舊。唯讀,供多日衍生
    (近3/5日法人累計、連漲天數、對昨收漲跌)用。缺日自然變短,不補。"""
    d = upto.isoformat() if isinstance(upto, _dt.date) else upto
    with conn(db_path) as c:
        rows = c.execute(
            f"SELECT * FROM {table} WHERE code=? AND data_date<=? "
            f"ORDER BY data_date DESC LIMIT ?", (code, d, n)).fetchall()
    return [dict(r) for r in rows]


def has_date(table: str, data_date: _dt.date | str, db_path: str = DB_PATH) -> int:
    """這一天抓過幾檔。0 = 完全沒抓過。"""
    d = data_date.isoformat() if isinstance(data_date, _dt.date) else data_date
    with conn(db_path) as c:
        return c.execute(
            f"SELECT COUNT(*) n FROM {table} WHERE data_date=?", (d,)
        ).fetchone()["n"]


def list_dates(table: str, limit: int = 90, db_path: str = DB_PATH) -> list[str]:
    """這張表有留痕的資料日,新→舊。供歷史複盤的日期下拉用(唯讀)。"""
    with conn(db_path) as c:
        rows = c.execute(
            f"SELECT DISTINCT data_date d FROM {table} "
            f"ORDER BY d DESC LIMIT ?", (limit,)).fetchall()
    return [r["d"] for r in rows]


# ---------------------------------------------------------------- 寫(受管制)

def write_rows(table: str, plugin: str, rows: Iterable[dict],
               db_path: str = DB_PATH) -> int:
    """
    INSERT OR IGNORE。同一天同一檔第二次寫入直接被擋掉,不覆蓋。
    非 owner 插件呼叫 → TableOwnershipError。
    """
    _check_owner(table, plugin)
    rows = list(rows)
    if not rows:
        return 0
    for r in rows:
        if "data_date" in r and "code" in r:
            _check_immutable(table, r["code"], r["data_date"], db_path)
    cols = list(rows[0].keys())
    ph = ",".join("?" * len(cols))
    sql = f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})"
    with conn(db_path) as c:
        cur = c.executemany(sql, [tuple(r[k] for k in cols) for r in rows])
        c.commit()
    return cur.rowcount


def upsert_intraday(table: str, plugin: str, rows: Iterable[dict],
                    db_path: str = DB_PATH) -> int:
    """
    盤中專用:當日資料可更新。過往交易日由 trigger 擋住。
    收盤後這些資料留在原地,不清空 —— 「已收盤」不等於「沒有資料」。
    """
    _check_owner(table, plugin)
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    ph = ",".join("?" * len(cols))
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({ph})"
    with conn(db_path) as c:
        cur = c.executemany(sql, [tuple(r[k] for k in cols) for r in rows])
        c.commit()
    return cur.rowcount


# ---------------------------------------------------------------- 取數(同一天只抓一次)

SourceFn = Callable[[list[str], _dt.date], list[dict]]


def fetch_once(table: str, plugin: str, codes: list[str], data_date: _dt.date,
               sources: list[tuple[str, SourceFn]],
               db_path: str = DB_PATH) -> dict[str, dict]:
    """
    同一天同一檔只抓一次。

    1. 先查 DB 這天抓過沒 → 抓過就直接回,零 API 呼叫。
    2. 沒抓過 → 依序試來源,誰先成功用誰,不比對、不重試其他來源。
       差幾百張甚至一千張都不影響「法人站買方還是賣方」的判斷。
    3. 寫入 DB,標 data_date 和 source,之後永不重抓。

    sources 依序:[("twse", fn), ("tpex", fn), ("finmind", fn)]
    官方 API 免費、無 rate limit,所以主力用官方,FinMind 當備援。
    """
    assert_can_read(data_date)

    cached = read_date(table, data_date, db_path)
    missing = [c for c in codes if c not in cached]
    if not missing:
        return cached  # 抓過了,零 API

    fetched_at = _dt.datetime.now().isoformat(timespec="seconds")
    for name, fn in sources:
        try:
            rows = fn(missing, data_date)
        except Exception as e:
            _log_fetch(table, "*", data_date, name, False, str(e)[:200], db_path)
            continue
        if not rows:
            _log_fetch(table, "*", data_date, name, False, "empty", db_path)
            continue
        for r in rows:
            r.setdefault("source", name)
            r.setdefault("fetched_at", fetched_at)
            r["data_date"] = data_date.isoformat()
        write_rows(table, plugin, rows, db_path)
        _log_fetch(table, "*", data_date, name, True, f"{len(rows)} rows", db_path)
        break  # 誰先回誰算數

    return read_date(table, data_date, db_path)


def _log_fetch(table: str, code: str, data_date: _dt.date, source: str,
               ok: bool, note: str, db_path: str = DB_PATH) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO fetch_log (table_name,code,data_date,source,ok,note,fetched_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (table, code, data_date.isoformat(), source, int(ok), note,
             _dt.datetime.now().isoformat(timespec="seconds")),
        )
        c.commit()


def fetch_count_today(db_path: str = DB_PATH) -> int:
    """今天實際打了幾次外部 API。重開服務後應該是 0。"""
    with conn(db_path) as c:
        return c.execute(
            "SELECT COUNT(*) n FROM fetch_log WHERE substr(fetched_at,1,10)=?",
            (today_tw().isoformat(),),
        ).fetchone()["n"]


# ---------------------------------------------------------------- 盤後指紋

def snapshot_post(data_date: _dt.date, db_path: str = DB_PATH) -> str:
    """把已驗證的盤後值存一份指紋。加插件之後比對,被動到就報錯。"""
    import hashlib
    parts = []
    for t in ("inst_flow", "margin", "daily_bar"):
        rows = read_date(t, data_date, db_path)
        parts.append(json.dumps(rows, sort_keys=True, default=str))
    blob = "|".join(parts)
    cs = hashlib.sha256(blob.encode()).hexdigest()
    n = sum(len(read_date(t, data_date, db_path))
            for t in ("inst_flow", "margin", "daily_bar"))
    with conn(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO post_checksum VALUES (?,?,?,?)",
            (data_date.isoformat(), cs, n,
             _dt.datetime.now().isoformat(timespec="seconds")),
        )
        c.commit()
    return cs


def verify_post(data_date: _dt.date, db_path: str = DB_PATH) -> tuple[bool, str]:
    with conn(db_path) as c:
        row = c.execute("SELECT * FROM post_checksum WHERE data_date=?",
                        (data_date.isoformat(),)).fetchone()
    if not row:
        return True, "尚無指紋,略過"
    import hashlib
    parts = []
    for t in ("inst_flow", "margin", "daily_bar"):
        parts.append(json.dumps(read_date(t, data_date, db_path),
                                sort_keys=True, default=str))
    cs = hashlib.sha256("|".join(parts).encode()).hexdigest()
    if cs != row["checksum"]:
        return False, f"盤後資料 {data_date} 被修改過(指紋不符)。有插件動到不該動的表。"
    return True, "指紋相符"
