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
from urllib.error import HTTPError
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TW_TZ = timezone(timedelta(hours=8))
BASE = Path(__file__).resolve().parent
CACHE_FILE = BASE / "chips_cache.json"
INST_DAYS = 20            # 近月 = 20 個交易日
HISTORY_DAYS = INST_DAYS + 1  # 讓盤前／18:00 切換仍能還原前一交易日的20日窗
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
    # 官方 CDN 偶爾會對 T86 回 307；短暫重試並補上正式尾斜線路徑，
    # 避免單次導向把整個 20 日窗口截斷成舊快取。
    urls = [url]
    if "twse.com.tw/rwd/zh/fund/T86?" in url:
        urls.append(url.replace("/T86?", "/T86/?"))
    retryable = {307, 308, 429, 500, 502, 503, 504}
    last_error = None
    for attempt in range(3):
        for candidate in urls:
            req = urllib.request.Request(candidate, headers={"User-Agent": UA})
            ctx = _TPEX_CTX if "tpex.org.tw" in candidate else None
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except HTTPError as exc:
                last_error = exc
                if exc.code not in retryable:
                    raise
        time.sleep(0.5 * (attempt + 1))
    raise last_error


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


def _lots(s):
    """Convert official share value to lots without turning missing into zero."""
    value = _num(s)
    return None if value is None else value / 1000


def _complete_lots(*values):
    """Return a complete lots sum; incomplete official rows stay unavailable."""
    parsed = [_lots(value) for value in values]
    return sum(parsed) if all(value is not None for value in parsed) else None


def _twse_day(d):
    """TWSE T86：某日全市場個股三大法人。回 {code: {foreign, trust, dealer, total}}（張）。"""
    url = ("https://www.twse.com.tw/rwd/zh/fund/T86"
           f"?response=json&date={d.strftime('%Y%m%d')}&selectType=ALLBUT0999")
    out = {}
    j = _get_json(url)
    if j.get("stat") != "OK" or not j.get("data"):
        return out
    for row in j["data"]:
        code = str(row[0]).strip()
        if not code.isdigit():
            continue
        # T86 欄位：[4]外陸資(不含自營) [7]外資自營商 [10]投信 [11]自營合計
        # [14]自營商自行買賣 [17]自營商避險 [-1]三大法人合計
        values = [_lots(row[index]) for index in (4, 7, 10, 11, 14, 17, -1)]
        if any(value is None for value in values):
            continue
        foreign = _complete_lots(row[4], row[7])
        out[code] = {
            "foreign": foreign,
            "trust": values[2],
            "dealer": values[3],
            "dealer_self": values[4],
            "dealer_hedge": values[5],
            "total": values[6],
        }
    return out


def _tpex_day(d):
    """TPEx（上櫃）某日三大法人買賣超。

    欄位（24 欄，單位：股）：
      [10] 外資及陸資合計買賣超  [13] 投信買賣超
      [16] 自營商自行買賣買賣超  [19] 自營商避險買賣超
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
            values = [_lots(row[index]) for index in (10, 13, 22, 16, 19, -1)]
            if any(value is None for value in values):
                continue
            out[code] = {
                "foreign": values[0],
                "trust": values[1],
                "dealer": values[2],
                "dealer_self": values[3],
                "dealer_hedge": values[4],
                "total": values[5],
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
    days = _trading_days_data(max_days=HISTORY_DAYS)
    if not days:
        print("[chips_official] 官方無任何交易日資料，快取未更新", flush=True)
        return 0
    # 5/20 日欄位只有在完整窗口取得時才可寫入；官方端暫時只回
    # 少數日期時，保留舊快取比把 3 日合計冒充 20 日安全。
    if len(days) < INST_DAYS:
        print(f"[chips_official] 官方資料窗口不足 {len(days)}/{INST_DAYS} 日，快取未更新",
              flush=True)
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
        metric_series = series[:INST_DAYS]
        net20 = round(sum(x["total"] for _, x in metric_series))

        def _streak(field):
            s = 0
            for _, x in series:                                    # 由最近往回
                v = x[field]
                if s == 0:
                    s = 1 if v > 0 else (-1 if v < 0 else 0)
                    if s == 0:
                        break
                elif s > 0 and v > 0:
                    s += 1
                elif s < 0 and v < 0:
                    s -= 1
                else:
                    break
            return s

        # 目前的欄位維持「最近 20 個交易日」語意；多抓的第 21 日只
        # 用來讓 asof 回看上一交易日時，仍有完整 20 日窗口可重算。
        series = metric_series
        streak = _streak("foreign")
        trust_streak = _streak("trust")
        institution_streak = _streak("total")
        latest_date, latest = series[0]
        recent3 = [x for _, x in series[:3]]
        recent5 = [x for _, x in series[:5]]
        foreign_abs_avg_20d = sum(abs(x["foreign"]) for _, x in series) / len(series)
        foreign_abnormal_threshold = max(3000, round(foreign_abs_avg_20d * 2))
        rec = dict(payload["stocks"].get(code) or {})
        rec.update({
            "inst_net_20d_lots": net20,
            "inst_streak": streak,
            "institution_streak": institution_streak,
            "trust_streak": trust_streak,
            "foreign_net_d": round(latest["foreign"]),
            "trust_net_d": round(latest["trust"]),
            "dealer_net_d": round(latest["dealer"]),
            "inst_net_d_lots": round(latest["total"]),
            "dealer_self_d": round(latest.get("dealer_self", 0)),
            "dealer_hedge_d": round(latest.get("dealer_hedge", 0)),
            "foreign_net_3d": round(sum(x["foreign"] for x in recent3)),
            "trust_net_3d": round(sum(x["trust"] for x in recent3)),
            "inst_net_3d_lots": round(sum(x["total"] for x in recent3)),
            "foreign_net_5d": round(sum(x["foreign"] for x in recent5)),
            "trust_net_5d": round(sum(x["trust"] for x in recent5)),
            "dealer_net_5d": round(sum(x["dealer"] for x in recent5)),
            "dealer_self_net_5d": round(sum(x.get("dealer_self", 0) for x in recent5)),
            "dealer_hedge_net_5d": round(sum(x.get("dealer_hedge", 0) for x in recent5)),
            "inst_net_5d_lots": round(sum(x["total"] for x in recent5)),
            "foreign_net_20d": round(sum(x["foreign"] for _, x in series)),
            "trust_net_20d": round(sum(x["trust"] for _, x in series)),
            "dealer_net_20d": round(sum(x["dealer"] for _, x in series)),
            "dealer_self_net_20d": round(sum(x.get("dealer_self", 0) for _, x in series)),
            "dealer_hedge_net_20d": round(sum(x.get("dealer_hedge", 0) for _, x in series)),
            "foreign": round(latest["foreign"]),
            "trust": round(latest["trust"]),
            "dealer": round(latest["dealer"]),
            # 「異常大買」只在相對自身近 20 個交易日的法人買賣超分布
            # 明顯放大時成立；沒有把固定張數門檻冒充成異常判定。
            "foreign_abs_avg_20d": round(foreign_abs_avg_20d, 1),
            "foreign_abnormal_threshold": foreign_abnormal_threshold,
            "foreign_abnormal_buy": latest["foreign"] >= foreign_abnormal_threshold,
            "foreign_abnormal_sell": latest["foreign"] <= -foreign_abnormal_threshold,
            # 保留 21 日官方原始序列，供盤前 asof=前一交易日精確重建
            # 單日、5 日與 20 日法人欄位，不使用較新的交易日覆蓋歷史。
            "inst_history": {date: dict(values) for date, values in
                             [(d, m[code]) for d, m in days if code in m]},
            "source": "TWSE T86 / TPEx 官方三大法人",
            "source_date": latest_date,
            "days_used": len(series),
        })
        payload["stocks"][code] = rec
        # 個股明細 API 會優先使用 detail:{code}；法人快取重建時必須
        # 同步覆蓋其中的單日／5日／20日欄位，避免舊的部分窗口殘留。
        detail_key = f"detail:{code}"
        detail = dict(payload["stocks"].get(detail_key) or {})
        for field in (
            "foreign_net_d", "trust_net_d", "dealer_net_d",
            "inst_net_d_lots", "dealer_self_d", "dealer_hedge_d", "foreign_net_3d",
            "trust_net_3d", "inst_net_3d_lots", "foreign_net_5d",
            "trust_net_5d", "dealer_net_5d", "dealer_self_net_5d",
            "dealer_hedge_net_5d", "inst_net_5d_lots",
            "foreign_net_20d", "trust_net_20d", "dealer_net_20d",
            "dealer_self_net_20d", "dealer_hedge_net_20d",
            "inst_net_20d_lots", "inst_streak", "trust_streak",
            "institution_streak", "foreign_abs_avg_20d",
            "foreign_abnormal_threshold", "foreign_abnormal_buy",
            "foreign_abnormal_sell",
            "source", "source_date", "days_used",
        ):
            if field in rec:
                detail[field] = rec[field]
        payload["stocks"][detail_key] = detail
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
