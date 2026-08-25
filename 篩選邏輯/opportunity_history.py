"""Sidecar 歷史資料層 —— **與 production DB 完全分離**。

⚠ 架構原則(2026-08-24 定案):

    FinMind historical cache
      → sidecar historical store(本檔)
      → 今天 51 檔 scoring
      → mls.db 只存最終 51 列結果

  **禁止**:historical cache → 兩萬多列 → mls.db

為什麼:
  · 正式 DB 不會隨歷史累積而變肥
  · 研究歷史可以整檔刪除重建,不影響正式引擎
  · 歷史資料的 bug 不會污染 production transaction state
  · 之後要改特徵算法,不需要動 production 資料結構

這個 store 的性質:唯讀、可重建、可整檔刪除、不參與任何 production transaction。
只提供 feature / statistics 計算所需的 OHLCV。

重建方式(在有 FinMind cache 的機器上):
    python3 -c "import opportunity_history as h; h.rebuild_from_csv('bars.csv')"
"""
from __future__ import annotations
import datetime as _dt
import os
import sqlite3
from typing import Optional

# 預設路徑:與 mls.db 同目錄但**是不同檔案**,可獨立刪除
DEFAULT_PATH = os.environ.get(
    "OPPORTUNITY_HISTORY_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "opportunity_history.db"))

DDL = """
CREATE TABLE IF NOT EXISTS hist_bar (
    code TEXT NOT NULL,
    data_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    source TEXT,
    PRIMARY KEY (code, data_date)
);
CREATE INDEX IF NOT EXISTS idx_hist_bar_code_date ON hist_bar(code, data_date);
CREATE TABLE IF NOT EXISTS hist_meta (k TEXT PRIMARY KEY, v TEXT);
"""

# ── Coverage contract 門檻 ────────────────────────────────────────
MIN_LOOKBACK_DAYS = 90      # 每檔至少要有的歷史交易日數
MAX_MISSING_RATIO = 0.10    # 相對於同期最完整股票的缺漏比例上限
MAX_STALENESS_DAYS = 10     # 歷史最新日與 production 資料日的最大落差(交易日約值)


def connect(path: str = DEFAULT_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def ensure(path: str = DEFAULT_PATH) -> None:
    with connect(path) as c:
        c.executescript(DDL)


def rebuild_from_rows(rows: list[tuple], path: str = DEFAULT_PATH) -> int:
    """rows = [(code, data_date, open, high, low, close, volume, source), ...]

    可重複執行:INSERT OR REPLACE。這個 store 本來就是可重建的。
    """
    ensure(path)
    build_id = _dt.datetime.now().strftime("sidecar-%Y%m%d-%H%M%S")
    with connect(path) as c:
        cur = c.executemany(
            "INSERT OR REPLACE INTO hist_bar "
            "(code,data_date,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,?)",
            rows)
        c.execute("INSERT OR REPLACE INTO hist_meta (k,v) VALUES ('build_id',?)", (build_id,))
        c.commit()
    return cur.rowcount


def build_id(path: str = DEFAULT_PATH) -> Optional[str]:
    """sidecar 版本識別 —— 存進每張 snapshot,之後才追得出當時用的是哪一版。"""
    if not os.path.exists(path):
        return None
    try:
        with connect(path) as c:
            r = c.execute("SELECT v FROM hist_meta WHERE k='build_id'").fetchone()
        return r["v"] if r else None
    except Exception:
        return None


def signal_days_from_bars(code: str, path: str = DEFAULT_PATH) -> set:
    """占位:frozen signal 的歷史觸發日由呼叫端算好後傳入。

    刻意不在此重算 —— sector 訊號需要同族群橫斷面,單檔 sidecar 讀不出來。
    """
    return set()


def read_bars(code: str, upto: str, limit: int = 400,
              path: str = DEFAULT_PATH) -> list[dict]:
    """讀單檔截至 upto(含)的最近 limit 根,**由舊到新**(ascending)。

    ⚠ 刻意與 store.read_recent 的「由新到舊」相反並在此明說 ——
      2026-08-24 上線時就是因為兩者順序不同而算出反向統計(已被護欄擋下)。
    """
    with connect(path) as c:
        rows = c.execute(
            "SELECT code,data_date,open,high,low,close,volume FROM hist_bar "
            "WHERE code=? AND data_date<=? ORDER BY data_date DESC LIMIT ?",
            (code, upto, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def coverage_contract(codes: list[str], production_date: str,
                      path: str = DEFAULT_PATH) -> dict:
    """每次 scoring 前的資料契約檢查。

    ⚠ 任何一項失敗:**只降級該股票為 INSUFFICIENT_HISTORY,
      不得讓整個盤後 pipeline 失敗。**

    回傳 {code: {"ok": bool, "reasons": [...], "n_days": int, ...}} 以及彙總。
    """
    if not os.path.exists(path):
        return {"_summary": {"store_missing": True, "path": path,
                             "ok_codes": 0, "total": len(codes)},
                **{c: {"ok": False, "reasons": ["sidecar store 不存在"], "n_days": 0}
                   for c in codes}}

    out: dict = {}
    counts = {}
    with connect(path) as c:
        for code in codes:
            row = c.execute(
                "SELECT COUNT(*) n, MIN(data_date) lo, MAX(data_date) hi, "
                "       COUNT(DISTINCT data_date) nd "
                "FROM hist_bar WHERE code=? AND data_date<=?",
                (code, production_date)).fetchone()
            n, lo, hi, nd = row["n"], row["lo"], row["hi"], row["nd"]
            counts[code] = n
            reasons = []
            if n < MIN_LOOKBACK_DAYS:
                reasons.append(f"歷史僅 {n} 日 < {MIN_LOOKBACK_DAYS}")
            if n != nd:
                reasons.append(f"有重複日期({n} 列 / {nd} 個日期)")
            if hi:
                gap = (_dt.date.fromisoformat(production_date)
                       - _dt.date.fromisoformat(hi)).days
                if gap > MAX_STALENESS_DAYS * 2:      # 行事曆日,寬鬆換算
                    reasons.append(f"歷史最新 {hi},落後 production {gap} 天")
            else:
                reasons.append("無任何歷史")
            # 排序檢查:確認讀出來確實遞增
            bars = read_bars(code, production_date, 5, path)
            dates = [b["data_date"] for b in bars]
            if dates != sorted(dates):
                reasons.append("讀出順序非由舊到新")
            out[code] = {"ok": not reasons, "reasons": reasons, "n_days": n,
                         "oldest": lo, "newest": hi}

    if counts:
        best = max(counts.values()) or 1
        for code, rec in out.items():
            miss = 1 - (counts[code] / best)
            rec["missing_ratio"] = round(miss, 4)
            if miss > MAX_MISSING_RATIO:
                rec["ok"] = False
                rec["reasons"].append(f"相對最完整股票缺漏 {miss:.1%}")

    ok_n = sum(1 for c in codes if out[c]["ok"])
    days = [out[c]["n_days"] for c in codes]
    out["_summary"] = {
        "store_missing": False, "path": path,
        "ok_codes": ok_n, "total": len(codes),
        "min_days": min(days) if days else 0,
        "max_days": max(days) if days else 0,
        "median_days": sorted(days)[len(days) // 2] if days else 0,
        "oldest": min((out[c]["oldest"] for c in codes if out[c]["oldest"]), default=None),
        "newest": max((out[c]["newest"] for c in codes if out[c]["newest"]), default=None),
    }
    return out
