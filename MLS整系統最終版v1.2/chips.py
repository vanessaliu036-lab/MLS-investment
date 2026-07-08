"""
MLS 標準版 — chips.py
籌碼資料層:法人近月買賣超、大戶(千張)持股比例。

【數據源事實】Shioaji 只有即時行情,沒有法人買賣超/股權分散 API。
本模組用 FinMind 盤後資料集(免費層即可,每日日更):
  - TaiwanStockInstitutionalInvestorsBuySell  三大法人買賣超(日)
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


def _compute_chips_for_date(code, target_date):
    """
    計算指定交易日(target_date, YYYY-MM-DD)的籌碼資料。
    與 get_chips 同邏輯,但不走當日快取、不動 _cache。
    用於盤後/歷史時序回看(避免用未來資料判過去,違反 NEXORA Hard Rule)。
    """
    result = {
        "inst_net_20d_lots": None, "inst_streak": None,
        "big_holder_pct": None, "big_holder_trend": None,
    }

    # ── 法人買賣超(以 target_date 為終點往前抓,取最近 20 交易日) ──
    try:
        tdt = datetime.strptime(target_date, "%Y-%m-%d")
        start = (tdt - timedelta(days=70)).strftime("%Y-%m-%d")
        rows = _finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start)
        by_date = {}
        for r in rows:
            if r.get("name") in ("Foreign_Investor", "Investment_Trust"):
                d = r["date"]
                if d > target_date:    # 排除 target_date 之後的資料(未來)
                    continue
                by_date.setdefault(d, {"net": 0, "foreign_net": 0})
                net = (r.get("buy", 0) - r.get("sell", 0)) / 1000
                by_date[d]["net"] += net
                if r["name"] == "Foreign_Investor":
                    by_date[d]["foreign_net"] += net
        dates = sorted(by_date.keys())[-INST_DAYS:]
        if dates:
            result["inst_net_20d_lots"] = round(sum(by_date[d]["net"] for d in dates))
            streak = 0
            for d in reversed(dates):
                f = by_date[d]["foreign_net"]
                if streak == 0:
                    streak = 1 if f > 0 else (-1 if f < 0 else 0)
                elif (streak > 0 and f > 0):
                    streak += 1
                elif (streak < 0 and f < 0):
                    streak -= 1
                else:
                    break
            result["inst_streak"] = streak
    except Exception as e:
        print(f"[chips@date] 法人 {code} {target_date} 失敗: {e}")

    # ── 大戶比例(取 target_date 當週或前一週) ─────────────
    try:
        tdt = datetime.strptime(target_date, "%Y-%m-%d")
        start = (tdt - timedelta(days=45)).strftime("%Y-%m-%d")
        rows = _finmind("TaiwanStockHoldingSharesPer", code, start)
        weeks = {}
        for r in rows:
            lvl = str(r.get("HoldingSharesLevel", ""))
            first = lvl.split("-")[0].replace(",", "")
            try:
                min_shares = int(first)
            except ValueError:
                continue
            if min_shares >= BIG_HOLDER_LEVEL * 1000:
                if r["date"] > target_date:    # 排除未來
                    continue
                weeks.setdefault(r["date"], 0)
                weeks[r["date"]] += float(r.get("percent", 0))
        wd = sorted(weeks.keys())
        if wd:
            result["big_holder_pct"] = round(weeks[wd[-1]], 2)
            if len(wd) >= 2:
                result["big_holder_trend"] = round(weeks[wd[-1]] - weeks[wd[0]], 2)
    except Exception as e:
        print(f"[chips@date] 大戶 {code} {target_date} 失敗: {e}")

    return result


def get_chips_at(code, date):
    """
    取得指定交易日的籌碼快照(時序對齊)。
    用於 after_hours / EOD 等「回看過去某日」的情境,
    避免 chips.get_chips() 拉到「現在」當下資料造成的未來洩漏。

    注意:此函式會呼叫 FinMind API(每日 300/hr 匿名額度);
          若族內個股 > 50 檔同一天回看,可能觸發額度上限。
    """
    return _compute_chips_for_date(code, date)


def get_chips(code):
    """
    回傳該股籌碼摘要 dict:
      inst_net_20d_lots   法人(外資+投信)近20日合計買賣超(張,+買超/-賣超)
      inst_streak         外資連續買超天數(負值=連賣)
      big_holder_pct      千張大戶持股比例(%)
      big_holder_trend    大戶比例近4週變化(百分點)
      查無資料時對應值為 None。結果快取至當日。
    """
    global _cache
    _load_disk()
    today = _today_key()
    if _cache.get("date") == today and code in _cache.get("stocks", {}):
        return _cache["stocks"][code]

    result = {
        "inst_net_20d_lots": None, "inst_streak": None,
        "big_holder_pct": None, "big_holder_trend": None,
    }

    # ── 法人買賣超(近40日抓,取最近20交易日) ──────────
    try:
        start = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
        rows = _finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start)
        # 欄位: date, stock_id, name(Foreign_Investor/Investment_Trust/...), buy, sell
        by_date = {}
        for r in rows:
            if r.get("name") in ("Foreign_Investor", "Investment_Trust"):
                d = r["date"]
                by_date.setdefault(d, {"net": 0, "foreign_net": 0})
                net = (r.get("buy", 0) - r.get("sell", 0)) / 1000  # 股→張
                by_date[d]["net"] += net
                if r["name"] == "Foreign_Investor":
                    by_date[d]["foreign_net"] += net
        dates = sorted(by_date.keys())[-INST_DAYS:]
        if dates:
            result["inst_net_20d_lots"] = round(sum(by_date[d]["net"] for d in dates))
            streak = 0
            for d in reversed(dates):
                f = by_date[d]["foreign_net"]
                if streak == 0:
                    streak = 1 if f > 0 else (-1 if f < 0 else 0)
                elif (streak > 0 and f > 0):
                    streak += 1
                elif (streak < 0 and f < 0):
                    streak -= 1
                else:
                    break
            result["inst_streak"] = streak
    except Exception as e:
        print(f"[chips] 法人 {code} 失敗: {e}")

    # ── 大戶比例(股權分散,週資料) ─────────────────────
    try:
        start = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
        rows = _finmind("TaiwanStockHoldingSharesPer", code, start)
        # 欄位: date, stock_id, HoldingSharesLevel, people, percent, unit
        # 千張大戶 = level "1,000,001-5,000,000" 以上各級距 percent 加總
        weeks = {}
        for r in rows:
            lvl = str(r.get("HoldingSharesLevel", ""))
            first = lvl.split("-")[0].replace(",", "")
            try:
                min_shares = int(first)
            except ValueError:
                continue  # 排除 "total" 等
            if min_shares >= BIG_HOLDER_LEVEL * 1000:  # 張→股
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
