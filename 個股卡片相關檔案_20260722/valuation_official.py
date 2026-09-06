# -*- coding: utf-8 -*-
"""官方本益比／股價淨值比快取建立器 — TWSE BWIBBU_ALL + TPEx 上櫃本益比分析，免費無上限。

為什麼需要這支：
  個股卡片(stock_card.py)之前完全沒有估值資料，技術面延伸(RSI/KD 超買)時
  講不出「延伸但便宜」跟「延伸但貴」的差別。P/E、P/B 是 TWSE/TPEx 官方
  免費資料，跟法人籌碼一樣一次回全市場，不必像分點資料那樣要花錢買
  籌碼商——沒有理由不接。

用法（跟 chips_official 同一個排程週期，盤前/盤後各建一次）：
    import valuation_official
    valuation_official.build_cache(codes)      # 寫入 valuation_cache.json

盤中只讀 valuation_cache.json，不重打官方端點。
"""

import json
import time
from urllib.error import HTTPError
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TW_TZ = timezone(timedelta(hours=8))
BASE = Path(__file__).resolve().parent
CACHE_FILE = BASE / "valuation_cache.json"
UA = "Mozilla/5.0 (compatible; MLS/1.0)"

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"


def _today():
    return datetime.now(TW_TZ).date()


def _get_json(url, timeout=20):
    retryable = {307, 308, 429, 500, 502, 503, 504}
    last_error = None
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = exc
            if exc.code not in retryable:
                raise
        time.sleep(0.5 * (attempt + 1))
    raise last_error


def _num(s):
    try:
        v = float(str(s).replace(",", "").strip())
        return v if v == v else None  # NaN 濾除
    except Exception:
        return None


def _roc_to_iso(d):
    """'1150904' → '2026-09-04'；官方偶爾回帶斜線格式一併處理。"""
    s = str(d).replace("/", "").strip()
    try:
        return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
    except Exception:
        return None


def build_cache(codes, merge=True):
    """建立／更新 valuation_cache.json。回傳成功檔數。"""
    codes = {str(c) for c in codes}
    found = {}

    try:
        for row in _get_json(TWSE_URL) or []:
            code = str(row.get("Code") or "")
            if code not in codes:
                continue
            found[code] = {
                "pe_ratio": _num(row.get("PEratio")),
                "pb_ratio": _num(row.get("PBratio")),
                "dividend_yield": _num(row.get("DividendYield")),
                "valuation_source": "TWSE BWIBBU_ALL",
                "valuation_source_date": _roc_to_iso(row.get("Date")),
            }
    except Exception as exc:
        print(f"[valuation_official] TWSE 估值讀取失敗:{exc}", flush=True)

    try:
        for row in _get_json(TPEX_URL) or []:
            code = str(row.get("SecuritiesCompanyCode") or "")
            if code not in codes or code in found:
                continue
            found[code] = {
                "pe_ratio": _num(row.get("PriceEarningRatio")),
                "pb_ratio": _num(row.get("PriceBookRatio")),
                "dividend_yield": _num(row.get("YieldRatio")),
                "valuation_source": "TPEx 上櫃本益比分析",
                "valuation_source_date": _roc_to_iso(row.get("Date")),
            }
    except Exception as exc:
        print(f"[valuation_official] TPEx 估值讀取失敗:{exc}", flush=True)

    if not found:
        print("[valuation_official] 官方兩端皆無可用資料，快取未更新", flush=True)
        return 0

    payload = {"date": _today().isoformat(), "stocks": {}}
    if merge and CACHE_FILE.exists():
        try:
            old = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(old.get("stocks"), dict):
                payload["stocks"] = old["stocks"]
        except Exception:
            pass
    payload["stocks"].update(found)

    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE_FILE)
    print(f"[valuation_official] ✅ 官方估值快取 {len(found)}/{len(codes)} 檔", flush=True)
    return len(found)


def get_valuation(code):
    """讀快取,查無資料回傳 None(不假造)。"""
    if not CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    rec = (payload.get("stocks") or {}).get(str(code))
    if not rec:
        return None
    out = dict(rec)
    out["valuation_cache_date"] = payload.get("date")
    return out
