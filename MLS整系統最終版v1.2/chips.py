"""
MLS 標準版 — chips.py
籌碼資料層:法人近月買賣超、大戶(千張)持股比例。

【數據源事實】Shioaji 只有即時行情,沒有法人買賣超/股權分散 API。
本模組採「官方優先、FinMind 補歷史」：
  - TWSE T86                                  個股三大法人最新日資料(官方)
  - TaiwanStockInstitutionalInvestorsBuySell  法人歷史序列(補20日/連買賣)
  - TaiwanStockHoldingSharesPer               集保股權分散(週)
環境變數: FINMIND_TOKEN(可留空,空 token 走匿名額度 300/hr)

快取策略:龍頭股才查,結果存記憶體+磁碟(chips_cache.json),
每日 15:00 後首次請求時刷新。盤中絕不重複打 API。
"""

import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

from config import INST_DAYS, BIG_HOLDER_LEVEL

FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
CACHE_FILE = os.path.join(os.path.dirname(__file__), "chips_cache.json")

_cache = {"date": "", "stocks": {}}


def _finmind(dataset, data_id, start_date):
    token = os.environ.get("FINMIND_TOKEN", "")
    q = urllib.parse.urlencode({
        "dataset": dataset, "data_id": data_id, "start_date": start_date,
    })
    req = urllib.request.Request(
        f"{FINMIND_DATA_URL}?{q}",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode()).get("data", [])


def _load_disk():
    global _cache
    try:
        with open(CACHE_FILE) as f:
            _cache = json.load(f)
    except Exception:
        pass


def _save_disk():
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(_cache, f, ensure_ascii=False)
    except Exception:
        pass


def _today_key():
    return datetime.now().strftime("%Y-%m-%d")


def _norm_source_date(value):
    """Normalize YYYYMMDD / YYYY-MM-DD source dates to YYYY-MM-DD."""
    if value is None:
        return None
    raw = str(value).strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw[:10] if len(raw) >= 10 else raw


def _latest_official_institutional(code):
    """Get latest published official daily institutional data, if available."""
    try:
        import official_source
        fn = getattr(official_source, "latest_stock_institutional", None)
        if fn is None:
            return None
        row = fn(code)
        if not row:
            return None
        if not any(row.get(k) is not None for k in
                   ("foreign_lots", "trust_lots", "dealer_lots", "total_lots")):
            return None
        row = dict(row)
        row["date"] = _norm_source_date(row.get("date"))
        return row
    except Exception as e:
        print(f"[chips] 官方法人 {code} 失敗,降級 FinMind: {e}")
        return None


def _official_is_fresh_enough(official, fallback_latest):
    """Never let an older official lookup overwrite a newer fallback row."""
    if not official or not official.get("date"):
        return False
    return fallback_latest is None or official["date"] >= fallback_latest


def _stamp_institutional_source(result, official=None, fallback_date=None):
    if official and official.get("date"):
        result["institutional_data_date"] = official["date"]
        result["institutional_source_type"] = "official"
        result["institutional_source"] = official.get("source") or "TWSE T86"
    else:
        result["institutional_data_date"] = fallback_date
        result["institutional_source_type"] = "finmind_basic" if fallback_date else "unavailable"
        result["institutional_source"] = "FinMind TaiwanStockInstitutionalInvestorsBuySell" if fallback_date else None
    result["institutional_checked_at"] = datetime.now().isoformat(timespec="seconds")


def get_chips(code):
    """
    回傳該股籌碼摘要 dict。最新單日法人以 TWSE T86 為最高優先，
    FinMind 僅補歷史序列與集保週資料。
    """
    global _cache
    _load_disk()
    today = _today_key()
    official = _latest_official_institutional(code)

    cached = (_cache.get("stocks", {}).get(code)
              if _cache.get("date") == today else None)
    if cached:
        # 舊版只以 calendar-day 快取，可能把前一交易日永久鎖成「今天」。
        # 只有在已是同一份官方資料時才直接返回；否則重算並允許官方覆蓋。
        if (official and cached.get("institutional_source_type") == "official"
                and cached.get("institutional_data_date") == official.get("date")):
            return cached
        if official is None and cached.get("institutional_source_type") == "official":
            return cached

    result = {
        "inst_net_20d_lots": None, "inst_streak": None,
        "big_holder_pct": None, "big_holder_trend": None,
        "institutional_data_date": None,
        "institutional_source_type": "unavailable",
        "institutional_source": None,
    }

    # ── 法人買賣超：FinMind 歷史 + 官方最新日覆蓋 ─────────
    fallback_latest = None
    try:
        start_date = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
        rows = _finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start_date)
        by_date = {}
        for r in rows:
            if r.get("name") in ("Foreign_Investor", "Investment_Trust"):
                d = _norm_source_date(r.get("date"))
                if not d:
                    continue
                by_date.setdefault(d, {"net": 0, "foreign_net": 0})
                net = (r.get("buy", 0) - r.get("sell", 0)) / 1000
                by_date[d]["net"] += net
                if r["name"] == "Foreign_Investor":
                    by_date[d]["foreign_net"] += net
        fallback_latest = max(by_date.keys()) if by_date else None

        use_official = official if _official_is_fresh_enough(official, fallback_latest) else None
        if use_official:
            d = use_official["date"]
            f = use_official.get("foreign_lots") or 0
            t = use_official.get("trust_lots") or 0
            by_date[d] = {"net": f + t, "foreign_net": f}

        dates = sorted(by_date.keys())[-INST_DAYS:]
        if dates:
            result["inst_net_20d_lots"] = round(sum(by_date[d]["net"] for d in dates))
            streak = 0
            for d in reversed(dates):
                f = by_date[d]["foreign_net"]
                if streak == 0:
                    streak = 1 if f > 0 else (-1 if f < 0 else 0)
                elif streak > 0 and f > 0:
                    streak += 1
                elif streak < 0 and f < 0:
                    streak -= 1
                else:
                    break
            result["inst_streak"] = streak
        _stamp_institutional_source(result, use_official, fallback_latest)
    except Exception as e:
        print(f"[chips] 法人 {code} 失敗: {e}")
        _stamp_institutional_source(result, official, fallback_latest)

    # ── 大戶比例(股權分散,週資料) ─────────────────────
    try:
        start_date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
        rows = _finmind("TaiwanStockHoldingSharesPer", code, start_date)
        weeks = {}
        for r in rows:
            lvl = str(r.get("HoldingSharesLevel", ""))
            first = lvl.split("-")[0].replace(",", "")
            try:
                min_shares = int(first)
            except ValueError:
                continue
            if min_shares >= BIG_HOLDER_LEVEL * 1000:
                weeks.setdefault(r["date"], 0)
                weeks[r["date"]] += float(r.get("percent", 0))
        wd = sorted(weeks.keys())
        if wd:
            result["big_holder_pct"] = round(weeks[wd[-1]], 2)
            if len(wd) >= 2:
                result["big_holder_trend"] = round(weeks[wd[-1]] - weeks[wd[0]], 2)
    except Exception as e:
        print(f"[chips] 大戶 {code} 失敗: {e}")

    if _cache.get("date") != today:
        _cache = {"date": today, "stocks": {}}
    _cache["stocks"][code] = result
    _save_disk()
    return result


# ════════════════════════════════════════════════════════
# v2.3 新增:個股資訊卡細項籌碼(get_chips 保持不變,零影響)
# ════════════════════════════════════════════════════════
def get_chips_detail(code):
    """
    資訊卡籌碼面。最新一日外資/投信/自營以 TWSE T86 為最高優先；
    FinMind 補 20 日序列，TDCC/FinMind 補週持股級距。
    """
    global _cache
    _load_disk()
    today = _today_key()
    key = f"detail:{code}"
    official = _latest_official_institutional(code)

    cached = (_cache.get("stocks", {}).get(key)
              if _cache.get("date") == today else None)
    if cached:
        if (official and cached.get("institutional_source_type") == "official"
                and cached.get("institutional_data_date") == official.get("date")):
            return cached
        if official is None and cached.get("institutional_source_type") == "official":
            return cached

    result = {"foreign_net_d": None, "trust_net_d": None, "dealer_net_d": None,
              "foreign_net_20d": None,
              "big400_pct": None, "big400_delta": None,
              "big1000_pct": None, "big1000_delta": None,
              "main_force_net": None,
              "institutional_data_date": None,
              "institutional_source_type": "unavailable",
              "institutional_source": None}

    # ── 三大法人單日 + 外資20日(日資料) ──────────────
    fallback_latest = None
    try:
        start_date = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
        rows = _finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start_date)
        by_date = {}
        for r in rows:
            d = _norm_source_date(r.get("date"))
            if not d:
                continue
            net = (r.get("buy", 0) - r.get("sell", 0)) / 1000
            nm = r.get("name", "")
            g = by_date.setdefault(d, {"f": 0, "t": 0, "dl": 0})
            if nm == "Foreign_Investor":
                g["f"] += net
            elif nm == "Investment_Trust":
                g["t"] += net
            elif nm.startswith("Dealer"):
                g["dl"] += net
        fallback_latest = max(by_date.keys()) if by_date else None

        use_official = official if _official_is_fresh_enough(official, fallback_latest) else None
        if use_official:
            d = use_official["date"]
            by_date[d] = {
                "f": use_official.get("foreign_lots") or 0,
                "t": use_official.get("trust_lots") or 0,
                "dl": use_official.get("dealer_lots") or 0,
            }

        dates = sorted(by_date.keys())
        if dates:
            last = by_date[dates[-1]]
            result["foreign_net_d"] = round(last["f"])
            result["trust_net_d"] = round(last["t"])
            result["dealer_net_d"] = round(last["dl"])
            result["foreign_net_20d"] = round(sum(by_date[d]["f"] for d in dates[-INST_DAYS:]))
        _stamp_institutional_source(result, use_official, fallback_latest)
    except Exception as e:
        print(f"[chips] 法人細項 {code} 失敗: {e}")
        _stamp_institutional_source(result, official, fallback_latest)

    # ── 大戶級距(集保週資料):400張 / 1000張 ─────────
    try:
        start_date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
        rows = _finmind("TaiwanStockHoldingSharesPer", code, start_date)
        w400, w1000 = {}, {}
        for r in rows:
            lvl = str(r.get("HoldingSharesLevel", ""))
            first = lvl.split("-")[0].replace(",", "")
            try:
                min_shares = int(first)
            except ValueError:
                continue
            pct = float(r.get("percent", 0))
            d = r["date"]
            if min_shares >= 400 * 1000:
                w400[d] = w400.get(d, 0) + pct
            if min_shares >= 1000 * 1000:
                w1000[d] = w1000.get(d, 0) + pct
        for weeks, pk, dk in ((w400, "big400_pct", "big400_delta"),
                              (w1000, "big1000_pct", "big1000_delta")):
            wd = sorted(weeks.keys())
            if wd:
                result[pk] = round(weeks[wd[-1]], 2)
                if len(wd) >= 2:
                    result[dk] = round(weeks[wd[-1]] - weeks[wd[0]], 2)
    except Exception as e:
        print(f"[chips] 大戶級距 {code} 失敗: {e}")

    if _cache.get("date") != today:
        _cache = {"date": today, "stocks": {}}
    _cache["stocks"][key] = result
    _save_disk()
    return result
