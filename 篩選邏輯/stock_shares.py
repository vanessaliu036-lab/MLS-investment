"""stock_shares.py — 已發行普通股數(流通股數)快取,供 Turnover(週轉率)計算。

資料來源(兩者皆為官方免費 OpenAPI,不需要 token、不需要付費帳號):
  上市 TWSE : https://openapi.twse.com.tw/v1/opendata/t187ap03_L
              欄位「已發行普通股數或TDR原股發行股數」
  上櫃 TPEx : https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O
              欄位 IssueShares

⚠ 2026-08-27:先前誤判「沒有流通股數資料源」而把 Turnover 標成 N/A,是錯的——
   我沒有實際去查就下結論。這兩支 API 都可以直接拿到已發行股數,而且是免費的。
   FinMind 的 TaiwanStockInfo 確實沒有股數欄位(只有 industry/name/type),
   所以之前只看 cache 就以為抓不到。

股數變動很慢(增資/減資才變),所以:
  · 快取進 stock_shares 表,不是每次算都打 API
  · 預設 30 天才更新一次(REFRESH_DAYS),盤中永遠讀快取,不做網路 I/O
  · 抓不到就是抓不到,回 None 讓上游顯示「—」——絕不用估算值頂替
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import sqlite3
import urllib.request

TABLE = "stock_shares"
REFRESH_DAYS = 30
TIMEOUT = 30

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    code TEXT PRIMARY KEY,
    issued_shares REAL,        -- 已發行普通股數(股)
    par_value REAL,            -- 每股面額(元),僅記錄
    market TEXT,               -- TWSE / TPEX
    source_date TEXT,          -- 官方資料日期
    fetched_at TEXT
);
"""


def ensure(db_path: str = "mls.db") -> None:
    with sqlite3.connect(db_path) as c:
        c.executescript(DDL)


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "mls-intraday/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return _json.loads(r.read().decode("utf-8"))


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def fetch_all() -> dict[str, dict]:
    """回傳 {code: {issued_shares, par_value, market, source_date}}。
    任一來源失敗只影響該來源,不整個炸掉(另一邊照樣更新)。"""
    out: dict[str, dict] = {}

    try:
        for r in _get_json(TWSE_URL):
            code = (r.get("公司代號") or "").strip()
            shares = _num(r.get("已發行普通股數或TDR原股發行股數"))
            if code and shares:
                out[code] = {"issued_shares": shares, "par_value": None,
                            "market": "TWSE", "source_date": (r.get("出表日期") or "").strip()}
    except Exception as e:
        print(f"[stock_shares] TWSE 取數失敗:{e}")

    try:
        for r in _get_json(TPEX_URL):
            code = (r.get("SecuritiesCompanyCode") or "").strip()
            shares = _num(r.get("IssueShares"))
            if code and shares:
                out[code] = {"issued_shares": shares, "par_value": None,
                            "market": "TPEX", "source_date": (r.get("Date") or "").strip()}
    except Exception as e:
        print(f"[stock_shares] TPEx 取數失敗:{e}")

    return out


def refresh(db_path: str = "mls.db", force: bool = False) -> dict:
    """更新快取。距離上次更新未滿 REFRESH_DAYS 且非 force → no-op(不打 API)。"""
    ensure(db_path)
    now = _dt.datetime.now()
    with sqlite3.connect(db_path) as c:
        last = c.execute(f"SELECT MAX(fetched_at) FROM {TABLE}").fetchone()[0]
    if last and not force:
        try:
            if (now - _dt.datetime.fromisoformat(last)).days < REFRESH_DAYS:
                return {"skipped": "cache fresh", "last": last}
        except ValueError:
            pass

    data = fetch_all()
    if not data:
        return {"written": 0, "error": "both sources failed — 保留舊快取不動"}

    stamp = now.isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as c:
        c.executemany(
            f"INSERT OR REPLACE INTO {TABLE} (code, issued_shares, par_value, market,"
            f" source_date, fetched_at) VALUES (?,?,?,?,?,?)",
            [(k, v["issued_shares"], v["par_value"], v["market"], v["source_date"], stamp)
             for k, v in data.items()])
        c.commit()
    return {"written": len(data), "fetched_at": stamp}


def load(db_path: str = "mls.db") -> dict[str, float]:
    """{code: issued_shares}。表不存在或空 → 回空 dict(上游顯示 —,不估算)。"""
    try:
        with sqlite3.connect(db_path) as c:
            rows = c.execute(
                f"SELECT code, issued_shares FROM {TABLE} WHERE issued_shares > 0").fetchall()
        return {r[0]: r[1] for r in rows}
    except sqlite3.Error:
        return {}


if __name__ == "__main__":
    import sys
    print(refresh(sys.argv[1] if len(sys.argv) > 1 else "mls.db", force="--force" in sys.argv))
