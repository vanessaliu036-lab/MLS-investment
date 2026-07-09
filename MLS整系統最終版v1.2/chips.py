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
        # schema 升級:舊 cache 缺新欄位 → 補 None,避免 frontend 拿到 undefined
        _NEW_KEYS = [
            "foreign_net_20d", "trust_net_20d", "dealer_net_20d", "main_force_net_20d",
            "foreign_5d", "foreign_10d", "inst_5d", "inst_10d",
            "holder_400_pct", "holder_400_trend",
        ]
        for code, v in (_cache.get("stocks") or {}).items():
            if isinstance(v, dict):
                for k in _NEW_KEYS:
                    v.setdefault(k, None)
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


def get_chips(code):
    """
    回傳該股籌碼摘要 dict:
      inst_net_20d_lots   法人(外資+投信)近20日合計買賣超(張,+買超/-賣超)
      inst_streak         外資連續買超天數(負值=連賣)
      big_holder_pct      千張大戶持股比例(%)
      big_holder_trend    大戶比例近4週變化(百分點)
      foreign_net_20d     外資近20日合計(張)
      trust_net_20d       投信近20日合計(張)
      dealer_net_20d      自營近20日合計(張)
      main_force_net_20d  主力 = 外資+投信+自營 近20日合計(張)
      foreign_5d / 10d    外資近5/10日合計(張),代理「N日資金流」
      holder_400_pct      400張以上大戶持股比例(%)
      holder_400_trend    400張大戶比例近4週變化(百分點)
      inst_5d / inst_10d  三大法人近5/10日合計(張)
    查無資料時對應值為 None。結果快取至當日。
    """
    _load_disk()
    today = _today_key()
    global _cache
    if _cache.get("date") == today and code in _cache.get("stocks", {}):
        return _cache["stocks"][code]

    result = {
        "inst_net_20d_lots": None, "inst_streak": None,
        "big_holder_pct": None, "big_holder_trend": None,
        "foreign_net_20d": None, "trust_net_20d": None, "dealer_net_20d": None,
        "main_force_net_20d": None,
        "foreign_5d": None, "foreign_10d": None,
        "inst_5d": None, "inst_10d": None,
        "holder_400_pct": None, "holder_400_trend": None,
    }

    # ── 法人買賣超(近70日抓,留 20 / 10 / 5 三個分窗) ──────
    try:
        start = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
        rows = _finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start)
        # 欄位: date, stock_id, name(Foreign_Investor/Investment_Trust/Dealer), buy, sell
        by_date = {}
        for r in rows:
            who = r.get("name")
            if who not in ("Foreign_Investor", "Investment_Trust", "Dealer"):
                continue
            d = r["date"]
            net_lots = (r.get("buy", 0) - r.get("sell", 0)) / 1000  # 股→張
            by_date.setdefault(d, {"foreign": 0, "trust": 0, "dealer": 0})
            if who == "Foreign_Investor":
                by_date[d]["foreign"] += net_lots
            elif who == "Investment_Trust":
                by_date[d]["trust"] += net_lots
            elif who == "Dealer":
                by_date[d]["dealer"] += net_lots
        dates = sorted(by_date.keys())
        if dates:
            last20 = dates[-20:]
            last10 = dates[-10:]
            last5 = dates[-5:]
            result["inst_net_20d_lots"] = round(
                sum(by_date[d]["foreign"] + by_date[d]["trust"] for d in last20))
            result["foreign_net_20d"] = round(sum(by_date[d]["foreign"] for d in last20))
            result["trust_net_20d"] = round(sum(by_date[d]["trust"] for d in last20))
            result["dealer_net_20d"] = round(sum(by_date[d]["dealer"] for d in last20))
            result["main_force_net_20d"] = round(
                sum(by_date[d]["foreign"] + by_date[d]["trust"] + by_date[d]["dealer"]
                    for d in last20))
            result["inst_5d"] = round(
                sum(by_date[d]["foreign"] + by_date[d]["trust"] + by_date[d]["dealer"]
                    for d in last5))
            result["inst_10d"] = round(
                sum(by_date[d]["foreign"] + by_date[d]["trust"] + by_date[d]["dealer"]
                    for d in last10))
            result["foreign_5d"] = round(sum(by_date[d]["foreign"] for d in last5))
            result["foreign_10d"] = round(sum(by_date[d]["foreign"] for d in last10))
            streak = 0
            for d in reversed(last20):
                f = by_date[d]["foreign"]
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
        # 400張以上 = level "400,001-..." 各級距 percent 加總
        weeks_big = {}        # 千張
        weeks_400 = {}        # 400張以上
        for r in rows:
            lvl = str(r.get("HoldingSharesLevel", ""))
            first = lvl.split("-")[0].replace(",", "")
            try:
                min_shares = int(first)
            except ValueError:
                continue
            d = r["date"]
            pct = float(r.get("percent", 0))
            weeks_big.setdefault(d, 0)
            weeks_400.setdefault(d, 0)
            if min_shares >= 1000 * 1000:           # 千張以上
                weeks_big[d] += pct
            if min_shares >= 400 * 1000:            # 400張以上(包含千張)
                weeks_400[d] += pct
        wd = sorted(weeks_big.keys())
        if wd:
            result["big_holder_pct"] = round(weeks_big[wd[-1]], 2)
            if len(wd) >= 2:
                result["big_holder_trend"] = round(weeks_big[wd[-1]] - weeks_big[wd[0]], 2)
        wd400 = sorted(weeks_400.keys())
        if wd400:
            result["holder_400_pct"] = round(weeks_400[wd400[-1]], 2)
            if len(wd400) >= 2:
                result["holder_400_trend"] = round(weeks_400[wd400[-1]] - weeks_400[wd400[0]], 2)
    except Exception as e:
        print(f"[chips] 大戶 {code} 失敗: {e}")

    if _cache.get("date") != today:
        _cache = {"date": today, "stocks": {}}
    _cache["stocks"][code] = result
    _save_disk()
    return result