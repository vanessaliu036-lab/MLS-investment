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
_official_margin_cache = {}
# 官方當日融資融券要收盤後才發布；只退到舊交易日的結果只快取這麼久（秒）。
_MARGIN_PENDING_TTL = 900
_official_share_cache = {}


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
    # Merge onto whatever is currently on disk rather than blind-overwriting.
    # chips_official.build_cache() writes the same file independently on its
    # own schedule; a naive overwrite here can wipe its richer per-stock
    # fields (or, on the first call of a new day, every other stock's entry)
    # with just this call's narrower result.
    try:
        try:
            with open(CACHE_FILE) as f:
                on_disk = json.load(f)
        except Exception:
            on_disk = {}
        merged_stocks = dict(on_disk.get("stocks") or {})
        merged_stocks.update(_cache.get("stocks") or {})
        payload = {"date": _cache.get("date"), "stocks": merged_stocks}
        with open(CACHE_FILE, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def _today_key():
    return datetime.now().strftime("%Y-%m-%d")


def _asof_limit(asof=None):
    """Return the inclusive date limit for a chip lookup.

    ``asof`` is a calendar cut-off, not necessarily a trading day (for
    example Monday's cut-off can be Sunday).  Each source therefore filters
    its own rows and uses the latest available row on or before this limit.
    """
    return str(asof or _today_key())[:10]


def _rows_on_or_before(rows, asof=None):
    """Keep dated source rows that are available at the requested cut-off."""
    limit = _asof_limit(asof)
    return [row for row in (rows or [])
            if str(row.get("date") or "")[:10] <= limit]


def _official_number(value, shares=False):
    """Parse official TWSE/TPEx numbers and normalize shares to lots."""
    if value in (None, ""):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
        return round(number / 1000) if shares else round(number)
    except (TypeError, ValueError):
        return None


def _official_json(url):
    """官方站(TWSE/TPEx)的免 token JSON 讀取。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "Chrome/120 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.twse.com.tw/zh/",
    })
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _official_shareholding_snapshot(asof=None):
    """TWSE MI_QFIIS 整市場外資持股比率（上市），FinMind 402 時的免費備援。

    跟融資融券一樣往前退到真的有 rows 的交易日，並以那天當來源日。
    """
    candidates = _trading_day_candidates(asof)
    cache_key = candidates[0]
    hit = _official_share_cache.get(cache_key)
    if hit:
        cached_at, cached_out, complete = hit
        if complete or (datetime.now().timestamp() - cached_at) < _MARGIN_PENDING_TTL:
            return cached_out

    out = {}
    for trade_date in candidates:
        ymd = trade_date.replace("-", "")
        try:
            payload = _official_json(
                "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS"
                f"?date={ymd}&selectType=ALLBUT0999&response=json")
        except Exception as exc:
            print(f"[chips] TWSE 外資持股 {trade_date} 失敗: {exc}")
            continue
        rows = payload.get("data") or []
        if not rows:
            continue
        for row in rows:
            try:
                code = str(row[0]).strip()
                out[code] = {
                    "foreign_share_pct": float(row[7]),
                    "foreign_share_remain_pct": float(row[6]),
                    "source_date": trade_date,
                }
            except (TypeError, ValueError, IndexError):
                continue
        break

    if not out:
        return out
    complete = max(r["source_date"] for r in out.values()) >= cache_key
    _official_share_cache[cache_key] = (datetime.now().timestamp(), out, complete)
    return out


def _trading_day_candidates(asof=None, back=8):
    """由 asof 往回列出候選交易日（只跳週末，不含國定假日表）。

    官方融資融券／借券的當日檔要收盤後才發布，遇到國定假日則整天沒有檔；
    只認 asof 當天會讓資料永遠卡在最後一次抓成功的日期，所以往前退到真的
    有 rows 的那一天為止，並以那天當來源日。
    """
    limit = _asof_limit(asof)
    try:
        day = datetime.strptime(limit, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return [limit]
    days = []
    while len(days) < back:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    return days


def _margn_rows(payload):
    """從 MI_MARGN 回應取出個股明細表；第一張表是全市場統計，不是個股。"""
    if not payload:
        return []
    for table in payload.get("tables") or []:
        if len(table.get("data") or []) > 100:
            return table["data"]
    return payload.get("data") or []


def _official_margin_snapshot(asof=None):
    """Fetch official margin/SBL snapshots once for the requested asof date.

    TWSE MI_MARGN (margin/short) + TWT93U (SBL) and TPEx's two JSON reports
    are market-wide, so one request covers every stock in the 51-stock pool.
    This is the no-token fallback when FinMind's per-stock margin dataset is
    unavailable.

    每個來源各自往前退到有資料的交易日，並帶回自己的 ``source_date``；
    上游不得替它補上 asof，否則會把舊餘額當成今天的數字。
    """
    candidates = _trading_day_candidates(asof)
    cache_key = candidates[0]
    hit = _official_margin_cache.get(cache_key)
    if hit:
        cached_at, cached_out, complete = hit
        # 抓到的就是 asof 當天 → 不會再變，長期有效；只抓到更早的交易日 →
        # 表示官方今天還沒發布，短 TTL 讓晚間發布後能自動接上。
        if complete or (datetime.now().timestamp() - cached_at) < _MARGIN_PENDING_TTL:
            return cached_out

    out = {}
    _get = _official_json

    def _merge_credit(rows, twse=False, date=None):
        for row in rows or []:
            code = str(row[0] if isinstance(row, list) else
                       row.get("股票代號") or row.get("代號") or "").strip()
            if not code:
                continue
            if isinstance(row, list) and twse:
                # TWSE MI_MARGN「融資融券彙總」: 融資前日/今日 = 5/6,
                # 融券前日/今日 = 11/12，單位已是張。
                # （舊版誤用 TWT93U 的 2/6 欄，那張表前半是「融券限額」、
                #   後半是「借券賣出」，根本沒有融資餘額。）
                values = {
                    "margin_prev": _official_number(row[5]),
                    "margin_balance": _official_number(row[6]),
                    "short_prev": _official_number(row[11]),
                    "short_balance": _official_number(row[12]),
                }
            else:
                # TPEx margin/balance: credit fields are already in lots.
                values = {
                    "margin_prev": _official_number(
                        row[2] if isinstance(row, list)
                        else row.get("前資餘額(張)") or row.get("融資前日餘額")),
                    "margin_balance": _official_number(
                        row[6] if isinstance(row, list)
                        else row.get("資餘額") or row.get("融資今日餘額")),
                    "short_prev": _official_number(
                        row[10] if isinstance(row, list)
                        else row.get("前券餘額(張)") or row.get("融券前日餘額")),
                    "short_balance": _official_number(
                        row[14] if isinstance(row, list)
                        else row.get("券餘額") or row.get("融券今日餘額")),
                }
            record = out.setdefault(code, {})
            record.update(values)
            if date:
                record["source_date"] = date

    def _merge_sbl(rows, date=None):
        for row in rows or []:
            code = str(row[0] if isinstance(row, list) else
                       row.get("股票代號") or row.get("代號") or "").strip()
            if not code:
                continue
            # TPEx margin/sbl: second group (indices 8..12) is shares.
            if isinstance(row, list):
                values = {
                    "sbl_prev": _official_number(row[8], shares=True),
                    "sbl_balance": _official_number(row[12], shares=True),
                }
            else:
                values = {}
            record = out.setdefault(code, {})
            record.update(values)
            if date and values:
                record["sbl_source_date"] = date

    twse_date = None
    for trade_date in candidates:
        ymd = trade_date.replace("-", "")
        margn = None
        for margn_url in (
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_MARGN"
            f"?date={ymd}&selectType=ALL&response=json",
            "https://www.twse.com.tw/exchangeReport/MI_MARGN"
            f"?date={ymd}&selectType=ALL&response=json",
        ):
            try:
                margn = _get(margn_url)
                if _margn_rows(margn):
                    break
            except Exception:
                continue
        rows = _margn_rows(margn)
        if rows:
            _merge_credit(rows, twse=True, date=trade_date)
            twse_date = trade_date
            break
    if not twse_date:
        print(f"[chips] TWSE MI_MARGN 近 {len(candidates)} 個交易日都沒有資料，改用 OpenAPI")
        try:
            # OpenAPI 是上市融資融券的整市場備援；數值單位已是張。
            # 它沒有帶資料日期，所以不標 source_date，寧可讓該欄顯示「—」也
            # 不冒充今天的餘額。
            twse_margin = _get(
                "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN")
            _merge_credit(twse_margin)
        except Exception as fallback_error:
            print(f"[chips] TWSE OpenAPI 融資融券失敗: {fallback_error}")

    for trade_date in candidates:
        # TWT93U 前半是融券限額、後半(8..12 欄)才是借券賣出餘額，單位為股。
        ymd = trade_date.replace("-", "")
        twse = None
        for twse_url in (
            "https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U"
            f"?date={ymd}&response=json",
            "https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U"
            f"?response=json&date={ymd}&selectType=ALLBUT0999",
        ):
            try:
                twse = _get(twse_url)
                if twse.get("data"):
                    break
            except Exception:
                continue
        if twse and twse.get("data"):
            _merge_sbl(twse.get("data") or [], date=trade_date)
            break

    for trade_date in candidates:
        try:
            date_arg = urllib.parse.quote(trade_date.replace("-", "/"), safe="")
            tpex_credit = _get(
                "https://www.tpex.org.tw/www/zh-tw/margin/balance"
                f"?date={date_arg}")
            tables = tpex_credit.get("tables") or []
            rows = (tables[0].get("data") if tables else []) or []
        except Exception as exc:
            print(f"[chips] TPEx 官方融資融券 {trade_date} 失敗: {exc}")
            continue
        if rows:
            _merge_credit(rows, date=trade_date)
            break

    for trade_date in candidates:
        try:
            date_arg = urllib.parse.quote(trade_date.replace("-", "/"), safe="")
            tpex_sbl = _get(
                "https://www.tpex.org.tw/www/zh-tw/margin/sbl"
                f"?date={date_arg}")
            tables = tpex_sbl.get("tables") or []
            rows = (tables[0].get("data") if tables else []) or []
        except Exception as exc:
            print(f"[chips] TPEx 官方借券餘額 {trade_date} 失敗: {exc}")
            continue
        if rows:
            _merge_sbl(rows, date=trade_date)
            break

    if not out:
        # 空結果不進快取：官方稍晚才發布時，同一個 process 還要能重抓。
        return out
    dates = [r.get("source_date") for r in out.values() if r.get("source_date")]
    complete = bool(dates) and max(dates) >= cache_key
    _official_margin_cache[cache_key] = (datetime.now().timestamp(), out, complete)
    return out


def summarize_finmind_institutional(rows, inst_days=INST_DAYS):
    """Normalize FinMind institutional rows to canonical lots.

    Canonical definitions:
      foreign = Foreign_Investor
      trust   = Investment_Trust
      dealer  = Dealer_self + Dealer_Hedging + other Dealer* rows
      institution = foreign + trust + dealer

    All net values are lots (張). ``inst_streak`` is kept for compatibility
    but its semantic is FOREIGN streak; callers must label it 外資連買/連賣.
    """
    by_date = {}
    for row in rows or []:
        date = row.get("date")
        name = row.get("name") or ""
        if not date:
            continue
        try:
            buy = float(row.get("buy") or 0)
            sell = float(row.get("sell") or 0)
        except (TypeError, ValueError):
            continue
        net = (buy - sell) / 1000.0
        item = by_date.setdefault(date, {
            "foreign": 0.0, "trust": 0.0, "dealer": 0.0,
            "dealer_self": 0.0, "dealer_hedge": 0.0,
        })
        if name == "Foreign_Investor":
            item["foreign"] += net
        elif name == "Investment_Trust":
            item["trust"] += net
        elif name == "Dealer_self":
            item["dealer"] += net
            item["dealer_self"] += net
        elif name == "Dealer_Hedging":
            item["dealer"] += net
            item["dealer_hedge"] += net
        elif name.startswith("Dealer") or name == "Foreign_Dealer_Self":
            item["dealer"] += net

    dates = sorted(by_date)[-int(inst_days):]
    if not dates:
        return {}

    def institutional(date):
        x = by_date[date]
        return x["foreign"] + x["trust"] + x["dealer"]

    def sum_field(field, count):
        return round(sum(by_date[d][field] for d in dates[-count:]))

    def sum_inst(count):
        return round(sum(institutional(d) for d in dates[-count:]))

    streak = 0
    for date in reversed(dates):
        value = by_date[date]["foreign"]
        if value > 0:
            if streak < 0:
                break
            streak += 1
        elif value < 0:
            if streak > 0:
                break
            streak -= 1
        else:
            break

    institution_streak = 0
    for date in reversed(dates):
        value = institutional(date)
        if value > 0:
            if institution_streak < 0:
                break
            institution_streak += 1
        elif value < 0:
            if institution_streak > 0:
                break
            institution_streak -= 1
        else:
            break

    latest = by_date[dates[-1]]
    foreign_abs_avg_20d = sum(abs(by_date[d]["foreign"]) for d in dates) / len(dates)
    foreign_abnormal_threshold = max(3000, round(foreign_abs_avg_20d * 2))
    return {
        "inst_net_20d_lots": sum_inst(inst_days),
        "inst_net_5d_lots": sum_inst(5),
        "inst_net_3d_lots": sum_inst(3),
        "inst_streak": streak,
        "institution_streak": institution_streak,
        "foreign_days": streak,
        "foreign_net_d": round(latest["foreign"]),
        "trust_net_d": round(latest["trust"]),
        "dealer_net_d": round(latest["dealer"]),
        "dealer_self_d": round(latest["dealer_self"]),
        "dealer_hedge_d": round(latest["dealer_hedge"]),
        "foreign_net_3d": sum_field("foreign", 3),
        "trust_net_3d": sum_field("trust", 3),
        "dealer_net_3d": sum_field("dealer", 3),
        "foreign_net_5d": sum_field("foreign", 5),
        "trust_net_5d": sum_field("trust", 5),
        "dealer_net_5d": sum_field("dealer", 5),
        "foreign_net_20d": sum_field("foreign", inst_days),
        "trust_net_20d": sum_field("trust", inst_days),
        "dealer_net_20d": sum_field("dealer", inst_days),
        "source": "FinMind TaiwanStockInstitutionalInvestorsBuySell",
        "source_date": dates[-1],
        "foreign_abs_avg_20d": round(foreign_abs_avg_20d, 1),
        "foreign_abnormal_threshold": foreign_abnormal_threshold,
        "foreign_abnormal_buy": latest["foreign"] >= foreign_abnormal_threshold,
        "foreign_abnormal_sell": latest["foreign"] <= -foreign_abnormal_threshold,
        "days_used": len(dates),
        "unit": "lots",
        "schema_version": "chip_ssot_v1",
    }


def _rollup_official_history(row, history, asof_limit):
    """從官方 21 日原始序列重建指定 asof 的法人窗口。"""
    dates = sorted(str(date)[:10] for date in history if str(date)[:10] <= asof_limit)
    if not dates:
        return None
    dates = dates[-INST_DAYS:]
    latest_date = dates[-1]

    def value(date, field):
        try:
            return float((history.get(date) or {}).get(field) or 0)
        except (TypeError, ValueError):
            return 0.0

    def total(date):
        return value(date, "total")

    def streak(field):
        result = 0
        for date in reversed(dates):
            current = value(date, field)
            if result == 0:
                result = 1 if current > 0 else (-1 if current < 0 else 0)
                if result == 0:
                    break
            elif (result > 0 and current > 0) or (result < 0 and current < 0):
                result += 1 if result > 0 else -1
            else:
                break
        return result

    latest = history.get(latest_date) or {}
    recent3, recent5 = dates[-3:], dates[-5:]
    out = dict(row)
    out.update({
        "foreign_net_d": round(value(latest_date, "foreign")),
        "trust_net_d": round(value(latest_date, "trust")),
        "dealer_net_d": round(value(latest_date, "dealer")),
        "dealer_self_d": round(value(latest_date, "dealer_self")),
        "dealer_hedge_d": round(value(latest_date, "dealer_hedge")),
        "foreign_net_3d": round(sum(value(date, "foreign") for date in recent3)),
        "trust_net_3d": round(sum(value(date, "trust") for date in recent3)),
        "inst_net_3d_lots": round(sum(total(date) for date in recent3)),
        "foreign_net_5d": round(sum(value(date, "foreign") for date in recent5)),
        "trust_net_5d": round(sum(value(date, "trust") for date in recent5)),
        "dealer_net_5d": round(sum(value(date, "dealer") for date in recent5)),
        "inst_net_5d_lots": round(sum(total(date) for date in recent5)),
        "foreign_net_20d": round(sum(value(date, "foreign") for date in dates)),
        "trust_net_20d": round(sum(value(date, "trust") for date in dates)),
        "dealer_net_20d": round(sum(value(date, "dealer") for date in dates)),
        "inst_net_20d_lots": round(sum(total(date) for date in dates)),
        "inst_streak": streak("foreign"),
        "trust_streak": streak("trust"),
        "institution_streak": streak("total"),
        "source_date": latest_date,
        "days_used": len(dates),
        "source": row.get("source") or "TWSE T86 / TPEx 官方三大法人",
    })
    average = sum(abs(value(date, "foreign")) for date in dates) / len(dates)
    threshold = max(3000, round(average * 2))
    out.update({
        "foreign_abs_avg_20d": round(average, 1),
        "foreign_abnormal_threshold": threshold,
        "foreign_abnormal_buy": value(latest_date, "foreign") >= threshold,
        "foreign_abnormal_sell": value(latest_date, "foreign") <= -threshold,
    })
    return out


def _official_detail(code, asof=None):
    """讀取排程建立的 TWSE/TPEx 官方籌碼快取。

    個股卡片與盤中觀察池必須使用同一份官方法人資料；FinMind 若尚未
    發布最新交易日，不能把前一週資料當成最新五日資料。
    """
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        row = (payload.get("stocks") or {}).get(str(code)) or {}
        asof_limit = _asof_limit(asof)
        # 官方快取在 18:00 後可能已切到今天；若頁面仍在盤前／盤中，
        # 用保存的第 21 日原始資料還原 asof，而不是退回舊 FinMind 數字。
        if row.get("source_date") and row.get("source_date") > asof_limit:
            row = _rollup_official_history(row, row.get("inst_history") or {}, asof_limit)
            if row is None:
                return None
        # 舊版官方快取可能尚未保存 5 日欄位；只要單日、20 日與連買
        # 資料齊全，就可安全提供，5 日欄位維持 None，絕不回退舊 FinMind。
        required = ("source_date", "inst_net_20d_lots", "foreign_net_20d",
                    "trust_net_20d", "dealer_net_20d", "inst_streak")
        # The cache can be one or more trading days behind a calendar asof
        # (e.g. Monday before the official Monday update).  It is valid when
        # it is the latest available cached row not later than the cut-off.
        if row.get("source_date") and row.get("source_date") > asof_limit:
            return None
        return row if all(row.get(k) is not None for k in required) else None
    except Exception:
        return None


def get_chips(code):
    """
    回傳該股籌碼摘要 dict:
      inst_net_20d_lots   三大法人(外資+投信+自營)近20日合計買賣超(張,+買超/-賣超)
      inst_streak         外資連續買超天數(負值=連賣)
      big_holder_pct      千張大戶持股比例(%)
      big_holder_trend    大戶比例近4週變化(百分點)
    查無資料時對應值為 None。結果快取至當日。
    v2.2 修正:補 global 宣告——舊版函式尾端 `_cache = {...}` 賦值
    使 _cache 被判為區域變數,開頭讀取即 UnboundLocalError,
    導致籌碼快取層從未正常運作(所有呼叫端只拿到例外)。
    """
    global _cache
    _load_disk()
    today = _today_key()
    cached = (_cache.get("stocks") or {}).get(code)
    # 舊版會把 API 失敗的 None 寫成「今日已完成」，之後整天永遠不重試。
    # 只有真的拿到外資連續性才算完成，避免 UI 永久顯示「外資：—」。
    if (_cache.get("date") == today and cached and
            cached.get("inst_streak") is not None):
        return cached

    result = {
        "inst_net_20d_lots": None, "inst_streak": None,
        "big_holder_pct": None, "big_holder_trend": None,
    }

    # ── 法人買賣超(近40日抓,取最近20交易日) ──────────
    try:
        start = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
        rows = _finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start)
        summary = summarize_finmind_institutional(rows)
        if summary:
            result.update(summary)
    except Exception as e:
        print(f"[chips] 法人 {code} 失敗: {e}")

    # ── 大戶持股：禁止用法人張數冒充百分比 ────────────────
    # get_chips() is the lightweight scoring path and does not fetch TDCC here.
    # Keep the field unavailable rather than pollute the schema with a lot-count
    # proxy.  UI detail uses its dedicated source/date path when available.
    result["big_holder_pct"] = None
    result["big_holder_trend"] = None

    if _cache.get("date") != today:
        _cache = {"date": today, "stocks": {}}
    # Merge onto the on-disk entry for this code (not just the in-memory
    # _cache, which the date-rollover above may have just cleared) so this
    # narrow scoring-path result never wipes chips_official's richer fields
    # (trust_net_20d, dealer_net_20d, source, source_date, ...) for the same
    # stock.
    try:
        with open(CACHE_FILE) as f:
            existing = (json.load(f).get("stocks") or {}).get(code) or {}
    except Exception:
        existing = {}
    _cache["stocks"][code] = {**existing, **result}
    _save_disk()
    return result


# ════════════════════════════════════════════════════════
# v2.3 新增:個股資訊卡細項籌碼(get_chips 保持不變,零影響)
# ════════════════════════════════════════════════════════
def _daily_source_floor(asof_limit):
    """融資融券／借券這種每日資料，這一輪最低可接受的來源日。

    當日檔要收盤後才發布，所以盤中只要求前一交易日；收盤後就要求當天，
    落後的快取一律重抓。集保是週資料，不套這條。
    """
    days = _trading_day_candidates(asof_limit)
    if asof_limit >= _today_key() and datetime.now().hour < 18:
        return days[1] if len(days) > 1 else days[0]
    return days[0]


def _daily_sources_fresh(cached, asof_limit):
    """快取裡的每日型籌碼來源日夠不夠新。

    以前只比對法人 source_date 就整包命中，法人一更新就等於替融資融券、
    借券的舊日期背書，讓它們永遠停在最後一次抓成功的那天。
    """
    floor = _daily_source_floor(asof_limit)
    for field in ("margin_source_date", "lending_source_date"):
        date = cached.get(field)
        if not date or str(date)[:10] < floor:
            return False
    return True


def get_chips_detail(code, asof=None):
    """
    資訊卡籌碼面。回傳 dict(查無資料的欄位為 None,不假造):
      foreign_net_d / trust_net_d / dealer_net_d  最新一日外資/投信/自營買賣超(張)
      foreign_net_20d                             外資近20日合計(張)
      foreign_net_5d / trust_net_5d / dealer_net_5d 近5日各法人合計(張)
      inst_net_5d_lots / inst_streak              近5日合計/外資連買天數
      big400_pct / big400_delta                   400張以上持股% / 近4週變化(pp)
      big1000_pct / big1000_delta                 千張大戶持股% / 近4週變化(pp)
      main_force_net                              主力(分點)= None,FinMind 免費層無此資料,
                                                  接 premium 籌碼商後由 chip_provider 供給
    資料週期誠實標記:法人=日資料;大戶級距=集保週資料。
    """
    global _cache
    _load_disk()
    today = _today_key()
    asof_limit = _asof_limit(asof)
    key = f"detail:{code}"
    official = _official_detail(code, asof=asof)
    cached = (_cache.get("stocks") or {}).get(key) or {}
    if (_cache.get("date") == today and key in _cache.get("stocks", {})
            and cached.get("source_date")
            and "margin_source_date" in cached
            and "inst_streak" in cached
            and "trust_net_20d" in cached
            and "dealer_self_d" in cached
            and "lending_source_date" in cached
            and "foreign_share_source_date" in cached
            and (not asof or cached.get("source_date") <= asof_limit)
            and _daily_sources_fresh(cached, asof_limit)
            and (not official or cached.get("source_date") == official.get("source_date"))):
        return cached

    result = {"foreign_net_d": None, "trust_net_d": None, "dealer_net_d": None,
              "dealer_self_d": None, "dealer_hedge_d": None,
              "inst_net_d_lots": None,
              "foreign_net_3d": None, "trust_net_3d": None, "inst_net_3d_lots": None,
              "foreign_net_5d": None, "trust_net_5d": None,
              "dealer_net_5d": None, "inst_net_5d_lots": None,
              "foreign_net_20d": None, "trust_net_20d": None,
              "dealer_net_20d": None, "inst_streak": None, "trust_streak": None,
              "source": None, "source_date": None,
              "big400_pct": None, "big400_delta": None,
              "big1000_pct": None, "big1000_delta": None,
              "main_force_net": None,
              "margin_change_d": None, "margin_change_5d": None,
              "margin_balance": None, "margin_source_date": None,
              "short_balance": None, "short_change_d": None,
              "short_change_5d": None, "short_margin_ratio": None,
              "lending_volume_d": None, "lending_source_date": None,
              "lending_volume_source_date": None,
              "lending_balance": None, "lending_balance_change_d": None,
              "foreign_share_pct": None, "foreign_share_change": None,
              "foreign_share_remain_pct": None, "foreign_share_source_date": None}

    # 法人正本更新時，不得連帶洗掉其他資料源仍有效的欄位。
    # 各來源有自己的發布日；只要既有欄位的來源日在 asof 內，就先保留，
    # 本輪若成功取得更新資料再覆蓋，FinMind 暫時 402 時也不會整欄歸零。
    independent_fields = (
        "margin_change_d", "margin_change_5d", "margin_balance",
        "margin_source_date", "short_balance", "short_change_d",
        "short_change_5d", "short_margin_ratio", "lending_volume_d",
        "lending_volume_source_date",
        "lending_source_date", "lending_balance", "lending_balance_change_d",
        "foreign_share_pct", "foreign_share_change",
        "foreign_share_remain_pct", "foreign_share_source_date",
    )
    for field in independent_fields:
        cached_date = cached.get(
            "foreign_share_source_date" if field.startswith("foreign_share")
            else "lending_volume_source_date" if field == "lending_volume_d"
            else "lending_source_date" if field.startswith("lending")
            else "margin_source_date")
        if cached.get(field) is not None and (
                not cached_date or str(cached_date)[:10] <= asof_limit):
            result[field] = cached[field]

    # ── 三大法人單日 + 滾動5/20日：官方快取優先 ─────────
    if official:
        result.update({
            "foreign_net_d": official.get("foreign_net_d", official.get("foreign")),
            "trust_net_d": official.get("trust_net_d", official.get("trust")),
            "dealer_net_d": official.get("dealer_net_d", official.get("dealer")),
            "dealer_self_d": official.get("dealer_self_d"),
            "dealer_hedge_d": official.get("dealer_hedge_d"),
            # 三大法人「單日合計」正本；缺此欄會讓下游只能用
            # foreign+trust+dealer 巧合同名欄位兜出合計，一改欄位命名就斷。
            "inst_net_d_lots": official.get("inst_net_d_lots"),
            "foreign_net_3d": official.get("foreign_net_3d"),
            "trust_net_3d": official.get("trust_net_3d"),
            "inst_net_3d_lots": official.get("inst_net_3d_lots"),
            "foreign_net_5d": official.get("foreign_net_5d"),
            "trust_net_5d": official.get("trust_net_5d"),
            "dealer_net_5d": official.get("dealer_net_5d"),
            "inst_net_5d_lots": official.get("inst_net_5d_lots"),
            "inst_streak": official.get("inst_streak"),
            "trust_streak": official.get("trust_streak"),
            "foreign_net_20d": official.get("foreign_net_20d"),
            "trust_net_20d": official.get("trust_net_20d"),
            "dealer_net_20d": official.get("dealer_net_20d"),
            "source": official.get("source") or "TWSE T86 / TPEx 官方三大法人",
            "source_date": official.get("source_date"),
        })
    else:
      try:
        start = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
        rows = _rows_on_or_before(
            _finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start),
            asof_limit)
        by_date = {}
        for r in rows:
            d = r["date"]
            net = (r.get("buy", 0) - r.get("sell", 0)) / 1000     # 股→張
            nm = r.get("name", "")
            g = by_date.setdefault(d, {"f": 0, "t": 0, "dl": 0, "ds": 0, "dh": 0})
            if nm == "Foreign_Investor":
                g["f"] += net
            elif nm == "Investment_Trust":
                g["t"] += net
            elif nm == "Dealer_self":                              # 自營商自行買賣
                g["dl"] += net
                g["ds"] += net
            elif nm == "Dealer_Hedging":                           # 自營商避險
                g["dl"] += net
                g["dh"] += net
            elif nm.startswith("Dealer") or nm == "Foreign_Dealer_Self":
                g["dl"] += net                                     # 其餘自營相關,計入合計不拆細項
        dates = sorted(by_date.keys())
        if dates:
            last = by_date[dates[-1]]
            result["source_date"] = dates[-1]
            result["source"] = "FinMind 盤後法人"
            result["foreign_net_d"] = round(last["f"])
            result["trust_net_d"] = round(last["t"])
            result["dealer_net_d"] = round(last["dl"])
            result["dealer_self_d"] = round(last["ds"])
            result["dealer_hedge_d"] = round(last["dh"])
            result["inst_net_d_lots"] = round(last["f"] + last["t"] + last["dl"])
            recent3 = dates[-3:]
            result["foreign_net_3d"] = round(sum(by_date[d]["f"] for d in recent3))
            result["trust_net_3d"] = round(sum(by_date[d]["t"] for d in recent3))
            result["inst_net_3d_lots"] = round(sum(
                by_date[d]["f"] + by_date[d]["t"] + by_date[d]["dl"]
                for d in recent3))
            recent5 = dates[-5:]
            result["foreign_net_5d"] = round(sum(by_date[d]["f"] for d in recent5))
            result["trust_net_5d"] = round(sum(by_date[d]["t"] for d in recent5))
            result["dealer_net_5d"] = round(sum(by_date[d]["dl"] for d in recent5))
            result["inst_net_5d_lots"] = round(sum(
                by_date[d]["f"] + by_date[d]["t"] + by_date[d]["dl"]
                for d in recent5))
            result["foreign_net_20d"] = round(
                sum(by_date[d]["f"] for d in dates[-INST_DAYS:]))
            result["trust_net_20d"] = round(
                sum(by_date[d]["t"] for d in dates[-INST_DAYS:]))
            result["dealer_net_20d"] = round(
                sum(by_date[d]["dl"] for d in dates[-INST_DAYS:]))
            result["inst_net_20d_lots"] = round(sum(
                by_date[d]["f"] + by_date[d]["t"] + by_date[d]["dl"]
                for d in dates[-INST_DAYS:]))

            def _streak(field):
                s = 0
                for d in reversed(dates):
                    v = by_date[d][field]
                    if s == 0:
                        s = 1 if v > 0 else (-1 if v < 0 else 0)
                    elif (s > 0 and v > 0) or (s < 0 and v < 0):
                        s += 1 if s > 0 else -1
                    else:
                        break
                return s
            result["inst_streak"] = _streak("f")
            result["trust_streak"] = _streak("t")
      except Exception as e:
        print(f"[chips] 法人細項 {code} 失敗: {e}")

    # ── 大戶級距(集保週資料):400張 / 1000張 ─────────
    # v2.4 修正:HoldingSharesPer 是付費 API;改用「近 20/40 日法人買超」
    # 當大戶級距代理。big400 = 近 20 日合計(中實戶級),big1000 = 近 40 日合計(大戶級)
    # **前端顯示時必須加單位 "張"**;key 名仍維持 big400_pct/big1000_pct 以免破壞既有前端。
    try:
        start = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
        rows = _finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start)
        by_date = {}
        for r in rows:
            if r.get("name") in ("Foreign_Investor", "Investment_Trust"):
                by_date.setdefault(r["date"], 0)
                by_date[r["date"]] += (r.get("buy", 0) - r.get("sell", 0)) / 1000
        ds = sorted(by_date.keys())
        if len(ds) >= 20:
            # 400 大戶級 = 近 20 日法人合計(短線中實戶)
            result["big400_pct"] = round(sum(by_date[d] for d in ds[-20:]))
            result["big400_delta"] = round(
                sum(by_date[d] for d in ds[-10:]) - sum(by_date[d] for d in ds[-20:-10]))
        if len(ds) >= 40:
            # 1000 大戶級 = 近 40 日法人合計(長線大戶)
            result["big1000_pct"] = round(sum(by_date[d] for d in ds[-40:]))
            result["big1000_delta"] = round(
                sum(by_date[d] for d in ds[-20:]) - sum(by_date[d] for d in ds[-40:-20]))
    except Exception as e:
        print(f"[chips] 大戶級距 {code} 失敗: {e}")

    # 官方快取沒有持股級距資料；不要讓 FinMind 舊日期的代理值混入
    # 8/4 法人卡片，缺資料比錯日期更誠實。
    if official:
        result["big400_pct"] = None
        result["big400_delta"] = None
        result["big1000_pct"] = None
        result["big1000_delta"] = None

    # ── 融資融券(日資料) ───────────────────────────────
    # FinMind 的 TaiwanStockMarginPurchaseShortSale 已改為付費資料集；
    # TaiwanDailyShortSaleBalances 是免費資料集，且同時提供融資融券餘額。
    # 先嘗試專用資料集，失敗或無資料時回退到每日餘額資料，避免整張卡
    # 因單一資料集付費而全部顯示「—」。
    short_balance_rows = None
    official_margin = None
    try:
        start = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
        rows = _rows_on_or_before(
            _finmind("TaiwanStockMarginPurchaseShortSale", code, start),
            asof_limit)
        if not rows:
            raise ValueError("專用融資融券資料集無資料，改用每日餘額")
        rows = sorted(rows, key=lambda r: r.get("date", ""))
        if rows:
            latest = rows[-1]
            result["margin_source_date"] = latest.get("date")
            result["margin_balance"] = latest.get("MarginPurchaseTodayBalance")
            result["margin_change_d"] = (
                (latest.get("MarginPurchaseTodayBalance") or 0)
                - (latest.get("MarginPurchaseYesterdayBalance") or 0)
            )
            result["short_balance"] = latest.get("ShortSaleTodayBalance")
            result["short_change_d"] = (
                (latest.get("ShortSaleTodayBalance") or 0)
                - (latest.get("ShortSaleYesterdayBalance") or 0)
            )
            if result["margin_balance"]:
                result["short_margin_ratio"] = round(
                    (result["short_balance"] or 0) / result["margin_balance"], 4)
            if len(rows) >= 6:
                result["margin_change_5d"] = (
                    (latest.get("MarginPurchaseTodayBalance") or 0)
                    - (rows[-6].get("MarginPurchaseTodayBalance") or 0)
                )
                result["short_change_5d"] = (
                    (latest.get("ShortSaleTodayBalance") or 0)
                    - (rows[-6].get("ShortSaleTodayBalance") or 0)
                )
    except Exception as e:
        print(f"[chips] 融資融券 {code} 失敗: {e}")
        try:
            official_margin = _official_margin_snapshot(asof_limit).get(str(code))
            if official_margin:
                margin_now = official_margin.get("margin_balance")
                margin_prev = official_margin.get("margin_prev")
                short_now = official_margin.get("short_balance")
                short_prev = official_margin.get("short_prev")
                # 沒有可信的資料日就不標日期；寧可顯示「—」也不冒充 asof 當天。
                result["margin_source_date"] = official_margin.get("source_date")
                result["margin_balance"] = margin_now
                result["margin_change_d"] = (
                    margin_now - margin_prev
                    if margin_now is not None and margin_prev is not None else None)
                result["short_balance"] = short_now
                result["short_change_d"] = (
                    short_now - short_prev
                    if short_now is not None and short_prev is not None else None)
                if margin_now:
                    result["short_margin_ratio"] = round(
                        (short_now or 0) / margin_now, 4)
            else:
                start = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
                short_balance_rows = _rows_on_or_before(
                    _finmind("TaiwanDailyShortSaleBalances", code, start),
                    asof_limit)
                short_balance_rows = sorted(
                    short_balance_rows, key=lambda r: r.get("date", ""))
            if short_balance_rows:
                latest = short_balance_rows[-1]
                margin_now = latest.get("MarginShortSalesCurrentDayBalance")
                margin_prev = latest.get("MarginShortSalesPreviousDayBalance")
                short_now = latest.get("SBLShortSalesCurrentDayBalance")
                short_prev = latest.get("SBLShortSalesPreviousDayBalance")
                result["margin_source_date"] = latest.get("date")
                result["margin_balance"] = margin_now
                if margin_now is not None and margin_prev is not None:
                    result["margin_change_d"] = margin_now - margin_prev
                result["short_balance"] = short_now
                if short_now is not None and short_prev is not None:
                    result["short_change_d"] = short_now - short_prev
                if margin_now:
                    result["short_margin_ratio"] = round(
                        (short_now or 0) / margin_now, 4)
                if len(short_balance_rows) >= 6:
                    old = short_balance_rows[-6]
                    old_margin = old.get("MarginShortSalesCurrentDayBalance")
                    old_short = old.get("SBLShortSalesCurrentDayBalance")
                    if margin_now is not None and old_margin is not None:
                        result["margin_change_5d"] = margin_now - old_margin
                    if short_now is not None and old_short is not None:
                        result["short_change_5d"] = short_now - old_short
        except Exception as fallback_error:
            print(f"[chips] 每日餘額回退 {code} 失敗: {fallback_error}")

    # ── 借券(成交量 + 賣出餘額,日資料) ─────────────────
    # TaiwanStockSecuritiesLending:單日逐筆借券成交,加總當日 volume = 借券成交量。
    # TaiwanDailyShortSaleBalances:官方每日借券賣出餘額(SBL),取當日餘額與日增減。
    try:
        start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        rows = _rows_on_or_before(
            _finmind("TaiwanStockSecuritiesLending", code, start), asof_limit)
        by_date_vol = {}
        for r in rows:
            by_date_vol[r["date"]] = by_date_vol.get(r["date"], 0) + (r.get("volume") or 0)
        if by_date_vol:
            latest_d = sorted(by_date_vol.keys())[-1]
            # 借券「成交量」跟借券「賣出餘額」是兩個不同節奏的事實：成交量當天
            # 就有、餘額要等官方收盤檔。共用一個 lending_source_date 會讓卡片
            # 標著今天、顯示的卻是昨天的餘額，所以各記各的日期。
            result["lending_volume_source_date"] = latest_d
            result["lending_volume_d"] = round(by_date_vol[latest_d] / 1000)  # 股→張
    except Exception as e:
        print(f"[chips] 借券成交 {code} 失敗: {e}")
    try:
        start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        if official_margin and official_margin.get("sbl_balance") is not None:
            bal = official_margin.get("sbl_balance")
            prev = official_margin.get("sbl_prev")
            result["lending_source_date"] = (official_margin.get("sbl_source_date")
                                             or official_margin.get("source_date"))
            result["lending_balance"] = bal
            if bal is not None and prev is not None:
                result["lending_balance_change_d"] = bal - prev
        else:
            if short_balance_rows is None:
                short_balance_rows = _rows_on_or_before(
                    _finmind("TaiwanDailyShortSaleBalances", code, start),
                    asof_limit)
            rows = short_balance_rows
            rows = sorted(rows, key=lambda r: r.get("date", ""))
            if rows:
                latest = rows[-1]
                bal = latest.get("SBLShortSalesCurrentDayBalance")
                prev = latest.get("SBLShortSalesPreviousDayBalance")
                result["lending_source_date"] = latest.get("date")
                result["lending_balance"] = round(bal / 1000) if bal is not None else None
                if bal is not None and prev is not None:
                    result["lending_balance_change_d"] = round((bal - prev) / 1000)
    except Exception as e:
        print(f"[chips] 借券餘額 {code} 失敗: {e}")

    # ── 外資持股結構(週資料,依實際公告日更新) ─────────
    try:
        start = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
        rows = _rows_on_or_before(
            _finmind("TaiwanStockShareholding", code, start), asof_limit)
        rows = sorted(rows, key=lambda r: r.get("date", ""))
        if rows:
            latest = rows[-1]
            result["foreign_share_source_date"] = latest.get("date")
            result["foreign_share_pct"] = latest.get("ForeignInvestmentSharesRatio")
            result["foreign_share_remain_pct"] = latest.get("ForeignInvestmentRemainRatio")
            if len(rows) >= 2:
                prev_pct = rows[-2].get("ForeignInvestmentSharesRatio")
                if result["foreign_share_pct"] is not None and prev_pct is not None:
                    result["foreign_share_change"] = round(
                        result["foreign_share_pct"] - prev_pct, 2)
    except Exception as e:
        print(f"[chips] 外資持股 {code} 失敗: {e}")
    if result.get("foreign_share_pct") is None:
        # FinMind 免費額度用盡(402)時，上市檔改吃 TWSE 官方；抓不到就維持 None，
        # 不要沿用上一輪的舊比率冒充今天。
        try:
            row = _official_shareholding_snapshot(asof_limit).get(str(code))
            if row:
                result["foreign_share_pct"] = row["foreign_share_pct"]
                result["foreign_share_remain_pct"] = row["foreign_share_remain_pct"]
                result["foreign_share_source_date"] = row["source_date"]
                prev_days = _trading_day_candidates(row["source_date"])
                prev = (_official_shareholding_snapshot(prev_days[1]).get(str(code))
                        if len(prev_days) > 1 else None)
                if prev and prev["source_date"] != row["source_date"]:
                    result["foreign_share_change"] = round(
                        row["foreign_share_pct"] - prev["foreign_share_pct"], 2)
        except Exception as exc:
            print(f"[chips] 官方外資持股 {code} 失敗: {exc}")

    # 法人、融資融券、借券、集保是不同發布節奏的資料集，分開採用各自
    # 的最新可用日期；某一來源落後時，只讓該來源缺少的欄位顯示「—」，
    # 不得因日期不同而清空其他來源已取得的資料。

    if _cache.get("date") != today:
        _cache = {"date": today, "stocks": {}}
    _cache["stocks"][key] = result
    _save_disk()
    return result
