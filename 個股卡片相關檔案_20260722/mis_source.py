# -*- coding: utf-8 -*-
"""mis_source.py — TWSE MIS 免金鑰盤中報價（Quote Gateway 的備援源）

定位（依 Vanessa 架構規劃）：
    Shioaji 只負責 aflow / tick_type；價、量、突破監控的「備援」走這裡。
    Shioaji 掛掉時，價量畫面仍繼續跑；aflow 老實標 UNAVAILABLE，不拿五檔偽裝。

來源：https://mis.twse.com.tw/stock/api/getStockInfo.jsp （官方 MIS，非第三方轉抓）
    一次可查多檔；回傳最新成交價 z、累積量 v、昨收 y、開高低、五檔 b/a、資料時間 t。

實測坑（2026-08-04 盤中驗證）：
    1. z（成交價）在兩筆成交之間常回 "-" → 用五檔買一/賣一中價補，再退昨收。
    2. 上櫃股要用 otc_ 前綴（例：3363 上詮）；上市用 tse_。本模組對每檔自動
       試 tse→otc 並快取命中前綴，不需維護交易所對照表。
    3. TWSE 標示每 5 秒最多 3 個 request；本模組分批（預設 20 檔/批）並自帶節流。

本模組只做「取數 + 正規化」，不做訂閱、不落地、不判斷來源優先——那是 Gateway 的事。
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error

_MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://mis.twse.com.tw/stock/index.jsp",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# 每檔命中過的交易所前綴快取：code -> "tse" | "otc"
_EX_CACHE: dict[str, str] = {}
_BATCH = 20            # 每批檔數（TWSE 節流保守值）
_HTTP_TIMEOUT = 12


def _num(v):
    """MIS 欄位常是字串或 '-'；轉 float，取不到回 None。"""
    try:
        if v is None:
            return None
        s = str(v).strip()
        if s in ("", "-", "0.0000") and s != "0":
            # 注意：真正的 0 很少見於價；'-' 與空字串一律當缺值
            if s in ("", "-"):
                return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _int(v):
    f = _num(v)
    return int(f) if f is not None else None


def _mid_from_book(s: dict):
    """五檔買一/賣一中價（z 缺值時的價格代理）。"""
    def first(key):
        raw = s.get(key) or ""
        parts = [p for p in str(raw).split("_") if p not in ("", "-")]
        return _num(parts[0]) if parts else None
    bid1, ask1 = first("b"), first("a")
    if bid1 and ask1:
        return round((bid1 + ask1) / 2, 4)
    return bid1 or ask1


def _parse_row(s: dict) -> dict | None:
    code = str(s.get("c") or "").strip()
    if not code:
        return None
    prev = _num(s.get("y"))                       # 昨收
    price = _num(s.get("z"))                       # 最新成交價
    price_src = "trade"
    if price is None:                             # z='-' → 用五檔中價，再退昨收
        price = _mid_from_book(s)
        price_src = "book_mid" if price is not None else price_src
    if price is None and prev is not None:
        price, price_src = prev, "prev_close"
    vol = _int(s.get("v"))                         # 累積成交量（張）
    chg = round((price - prev) / prev * 100, 2) if (price and prev) else None
    return {
        "code": code,
        "price": price,
        "prev_close": prev,
        "change_rate": chg,
        "total_volume": vol or 0,
        "open": _num(s.get("o")), "high": _num(s.get("h")), "low": _num(s.get("l")),
        "mis_time": s.get("t"),                    # 交易所資料時間 HH:MM:SS
        "price_kind": price_src,                   # trade / book_mid / prev_close
        "source": "twse_mis",
    }


def _fetch(ex_ch: str) -> list[dict]:
    url = f"{_MIS_URL}?ex_ch={ex_ch}&json=1&delay=0&_={int(time.time()*1000)}"
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        d = json.loads(r.read().decode("utf-8"))
    if str(d.get("rtcode")) not in ("0000", "0"):
        return []
    return d.get("msgArray") or []


def fetch(codes: list[str]) -> dict[str, dict]:
    """回傳 {code: normalized_row}。缺值的檔不出現在結果裡（呼叫端沿用舊值/標記）。"""
    out: dict[str, dict] = {}
    pending = [str(c) for c in codes]

    # 第一輪：用快取前綴（未知者預設 tse）打一次
    for i in range(0, len(pending), _BATCH):
        batch = pending[i:i + _BATCH]
        ex_ch = "|".join(f"{_EX_CACHE.get(c, 'tse')}_{c}.tw" for c in batch)
        try:
            for s in _fetch(ex_ch):
                row = _parse_row(s)
                if row:
                    out[row["code"]] = row
                    ex = str(s.get("ex") or "").lower()   # 'tse'|'otc'
                    if ex in ("tse", "otc"):
                        _EX_CACHE[row["code"]] = ex
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"[mis] batch fail: {type(e).__name__}: {e}", flush=True)
        time.sleep(0.4)

    # 第二輪：第一輪沒回的（多半是上櫃被當 tse 查空）改 otc 重試一次
    missing = [c for c in pending if c not in out]
    for i in range(0, len(missing), _BATCH):
        batch = missing[i:i + _BATCH]
        ex_ch = "|".join(f"otc_{c}.tw" for c in batch)
        try:
            for s in _fetch(ex_ch):
                row = _parse_row(s)
                if row:
                    out[row["code"]] = row
                    _EX_CACHE[row["code"]] = "otc"
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"[mis] otc retry fail: {type(e).__name__}: {e}", flush=True)
        time.sleep(0.4)

    return out


if __name__ == "__main__":
    import sys
    cs = sys.argv[1].split(",") if len(sys.argv) > 1 else ["2049", "2383", "1815", "3363", "2492"]
    t0 = time.time()
    r = fetch(cs)
    print(f"fetched {len(r)}/{len(cs)} in {time.time()-t0:.1f}s")
    for c in cs:
        row = r.get(c)
        if row:
            print(f"  {c} {row['price']}({row['price_kind']}) chg={row['change_rate']} "
                  f"vol={row['total_volume']} t={row['mis_time']} ex={_EX_CACHE.get(c)}")
        else:
            print(f"  {c} —（未取得）")
