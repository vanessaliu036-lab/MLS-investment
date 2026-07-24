# -*- coding: utf-8 -*-
"""官方籌碼快取建立器 — TWSE T86 + TPEx 三大法人，免費無上限。

為什麼需要這支：
  chips.py 走 FinMind，單檔一次請求，51 檔就 51 次；免費額度一天就爆
  （HTTP 402 Payment Required），導致盤中「法人連買」大量缺資料、分數失真。

官方來源一次回「整個市場」某一天的所有個股法人買賣超，抓 N 個交易日
只要 N 次請求（不是 N×51），而且 TWSE/TPEx 官方免費無上限。

用法（盤前 08:30 / 盤後 18:05 由 server 排程呼叫）：
    import chips_official
    chips_official.build_cache(codes)      # 寫入 chips_cache.json

盤中只讀 chips_cache.json，不呼叫本模組。
"""

import json
import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TW_TZ = timezone(timedelta(hours=8))
BASE = Path(__file__).resolve().parent
CACHE_FILE = BASE / "chips_cache.json"
INST_DAYS = 20            # 近月 = 20 個交易日
LOOKBACK_DAYS = 34        # 往回找的日曆天數（含假日）
UA = "Mozilla/5.0 (compatible; MLS/1.0)"


def _today():
    return datetime.now(TW_TZ).date()


# TPEx 憑證缺 Subject Key Identifier，新版 OpenSSL 會驗不過。
# 這是公開唯讀的政府行情資料，且僅用於 tpex.org.tw；不放寬其他站台。
_TPEX_CTX = ssl.create_default_context()
_TPEX_CTX.check_hostname = False
_TPEX_CTX.verify_mode = ssl.CERT_NONE


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = _TPEX_CTX if "tpex.org.tw" in url else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


def _twse_day(d):
    """TWSE T86：某日全市場個股三大法人。回 {code: {foreign, trust, dealer, total}}（張）。"""
    url = ("https://www.twse.com.tw/rwd/zh/fund/T86"
           f"?response=json&date={d.strftime('%Y%m%d')}&selectType=ALL")
    out = {}
    j = _get_json(url)
    if j.get("stat") != "OK" or not j.get("data"):
        return out
    for row in j["data"]:
        code = str(row[0]).strip()
        if not code.isdigit():
            continue
        # T86 欄位：[4]外陸資(不含自營) [7]外資自營商 [10]投信 [11]自營合計 [-1]三大法人合計
        foreign = ((_num(row[4]) or 0) + (_num(row[7]) or 0)) / 1000
        out[code] = {
            "foreign": foreign,
            "trust": (_num(row[10]) or 0) / 1000,
            "dealer": (_num(row[11]) or 0) / 1000,
            "total": (_num(row[-1]) or 0) / 1000,
        }
    return out


def _tpex_day(d):
    """TPEx（上櫃）某日三大法人買賣超。

    欄位（24 欄，單位：股）：
      [10] 外資及陸資合計買賣超  [13] 投信買賣超
      [22] 自營商合計買賣超      [-1] 三大法人合計
    """
    url = ("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
           f"?type=Daily&sect=EW&date={d.strftime('%Y/%m/%d')}&response=json")
    out = {}
    try:
        j = _get_json(url)
    except Exception as exc:
        print(f"[chips_official] TPEx {d} 失敗:{exc}", flush=True)
        return out
    tables = j.get("tables") or []
    rows = tables[0].get("data") if tables else (j.get("aaData") or j.get("data") or [])
    for row in rows or []:
        try:
            code = str(row[0]).strip()
            if not code.isdigit() or len(code) != 4:
                continue
            out[code] = {
                "foreign": (_num(row[10]) or 0) / 1000,
                "trust": (_num(row[13]) or 0) / 1000,
                "dealer": (_num(row[22]) or 0) / 1000,
                "total": (_num(row[-1]) or 0) / 1000,
            }
        except Exception:
            continue
    return out


def _trading_days_data(max_days=INST_DAYS):
    """由今日往回抓，收集 max_days 個有資料的交易日（新→舊）。"""
    days = []
    d = _today()
    for _ in range(LOOKBACK_DAYS):
        if len(days) >= max_days:
            break
        if d.weekday() < 5:                       # 跳過週末
            try:
                twse = _twse_day(d)
            except Exception as exc:
                print(f"[chips_official] TWSE {d} 失敗:{exc}", flush=True)
                twse = {}
            if twse:
                merged = dict(twse)
                merged.update(_tpex_day(d))       # 上櫃補進來
                days.append((d.isoformat(), merged))
                time.sleep(0.6)                   # 對官方站客氣一點
        d -= timedelta(days=1)
    return days


def build_cache(codes, merge=True):
    """建立 / 更新 chips_cache.json 的法人欄位。回傳成功檔數。"""
    codes = [str(c) for c in codes]
    days = _trading_days_data()
    if not days:
        print("[chips_official] 官方無任何交易日資料，快取未更新", flush=True)
        return 0

    payload = {"date": _today().isoformat(), "stocks": {}}
    if merge and CACHE_FILE.exists():
        try:
            old = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(old.get("stocks"), dict):
                payload["stocks"] = old["stocks"]
        except Exception:
            pass

    ok = 0
    for code in codes:
        series = [(d, m[code]) for d, m in days if code in m]      # 新→舊
        if not series:
            continue
        net20 = round(sum(x["total"] for _, x in series))
        streak = 0
        for _, x in series:                                        # 由最近往回
            f = x["foreign"]
            if streak == 0:
                streak = 1 if f > 0 else (-1 if f < 0 else 0)
                if streak == 0:
                    break
            elif streak > 0 and f > 0:
                streak += 1
            elif streak < 0 and f < 0:
                streak -= 1
            else:
                break
        latest_date, latest = series[0]
        rec = dict(payload["stocks"].get(code) or {})
        rec.update({
            "inst_net_20d_lots": net20,
            "inst_streak": streak,
            "foreign_net_20d": round(sum(x["foreign"] for _, x in series)),
            "foreign": round(latest["foreign"]),
            "trust": round(latest["trust"]),
            "dealer": round(latest["dealer"]),
            "source": "TWSE T86 / TPEx 官方三大法人",
            "source_date": latest_date,
            "days_used": len(series),
        })
        payload["stocks"][code] = rec
        ok += 1

    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE_FILE)
    print(f"[chips_official] ✅ 官方籌碼快取 {ok}/{len(codes)} 檔"
          f"（{len(days)} 個交易日，資料日 {days[0][0]}）", flush=True)
    return ok


if __name__ == "__main__":
    import config as C
    build_cache(list(getattr(C, "UNIVERSE", [])))
