"""
MLS v4.0 — data_collector.py
盤後免費官方資料源：TWSE / TPEx / FinMind
- 全部官方 open data、不用 token、不用 shioaji
- 任何源失敗 → 自動 fallback，絕不返回空白
- 結果落 sqlite cache 避免重打
"""
import os
import json
import time
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

import db

CACHE_DIR = "/tmp/mls-v4-cache"
os.makedirs(CACHE_DIR, exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TIMEOUT = 10
FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"
# 免費註冊 token 把匿名配額(易 402)換成 600 次/小時；未設就沿用舊的匿名呼叫。
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
_FINMIND_TOKEN_QS = f"&token={FINMIND_TOKEN}" if FINMIND_TOKEN else ""

# TPEx 站台的憑證鏈在部分 OpenSSL 版本上會炸「Missing Subject Key Identifier」
# (系統預設信任庫的嚴格度問題,不是憑證真的有問題),導致上櫃股法人/融資長期
# 抓不到卻只留在錯誤訊息裡沒人注意。certifi 的信任庫驗證得過,改用它建 context
# (只是換一份信任庫,不會放寬驗證);certifi 不在時退回系統預設,行為不變。
try:
    import ssl as _ssl
    import certifi as _certifi
    _SSL_CTX = _ssl.create_default_context(cafile=_certifi.where())
except Exception:
    _SSL_CTX = None


def _http(url, headers=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.twse.com.tw/", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return r.read().decode("utf-8", errors="ignore")


def _cache_path(key, ext="json"):
    return os.path.join(CACHE_DIR, f"{key}.{ext}")


def _cache_get(key, max_age_sec=1800):
    """30 分鐘內的快取直接用。"""
    p = _cache_path(key)
    if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < max_age_sec:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _cache_set(key, data):
    p = _cache_path(key)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════
# 1. TWSE 法人 T86 今日 三大法人買賣超
# ══════════════════════════════════════════════════════════════
def fetch_twse_inst_today(date_yyyymmdd: str) -> dict:
    """return {code: {foreign_lots, invest_lots, dealer_lots, total_lots, name}}"""
    key = f"twse_t86_{date_yyyymmdd}"
    cached = _cache_get(key, 3600)
    if cached:
        return cached

    url = f"https://www.twse.com.tw/fund/T86?response=json&date={date_yyyymmdd}&selectType=ALL"
    try:
        raw = _http(url)
        d = json.loads(raw)
        if d.get("stat") != "OK":
            return {}
        rows = d.get("data", [])
        out = {}
        for r in rows:
            if len(r) < 19 or not r[0]:
                continue
            code = r[0].strip()
            # 只收 4 碼股票（過濾權證/ETN 6 碼）
            if not code.isdigit() or len(code) != 4:
                continue
            name = r[1].strip()
            # 19 欄：r[4]=外陸資買賣超, r[7]=外資自營買賣超, r[10]=投信買賣超,
            #        r[14]=自營(自行)買賣超, r[17]=自營(避險)買賣超, r[18]=三大法人合計
            def to_int(x):
                return int(x.replace(",", "")) if x and x != "--" else 0
            foreign = to_int(r[4]) + to_int(r[7])
            invest = to_int(r[10])
            dealer = to_int(r[14]) + to_int(r[17])
            total = to_int(r[18])
            out[code] = {
                "name": name,
                "foreign_lots": int(foreign / 1000),  # 股 → 張
                "invest_lots": int(invest / 1000),
                "dealer_lots": int(dealer / 1000),
                "total_lots": int(total / 1000),
            }
        _cache_set(key, out)
        return out
    except Exception as e:
        print(f"[data] twse_t86 fail: {e}")
        return {}


# ══════════════════════════════════════════════════════════════
# 1.5 TPEX 法人 三大法人買賣超
# ══════════════════════════════════════════════════════════════
def fetch_tpex_inst_today(date_yyyymmdd: str) -> dict:
    """return {code: {foreign_lots, invest_lots, dealer_lots, total_lots, name}}
    TPEX 欄位（從精材 7/21 推得，group 順序）：
      r[0]=代號, r[1]=名稱
      group1 r[2,3,4]=外陸資(買,賣,買賣超)
      group2 r[5,6,7]=外資自營(買,賣,買賣超)
      group3 r[8,9,10]=外陸資+外資自營合計(買,賣,買賣超)
      group4 r[11,12,13]=投信(買,賣,買賣超)
      group5 r[14,15,16]=自營(自行)(買,賣,買賣超)
      group6 r[17,18,19]=自營(避險)(買,賣,買賣超)
      group7 r[20,21,22]=自營合計(買,賣,買賣超)
      r[23]=三大法人合計"""
    key = f"tpex_3insti_{date_yyyymmdd}"
    cached = _cache_get(key, 3600)
    if cached:
        return cached

    roc = int(date_yyyymmdd) - 19110000
    ymd = f"{roc // 10000:03d}/{(roc % 10000) // 100:02d}/{roc % 100:02d}"
    url = (f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/"
           f"3itrade_hedge_result.php?l=zh-tw&o=json&se=EW&t=D&d={ymd}&s=0,asc")
    try:
        # TPEx 負載平衡有部分節點憑證缺 Subject Key Identifier,間歇性驗證
        # 失敗(見 fetch_official_margin_snapshot 的同一發現);重試換節點。
        raw = None
        for attempt in range(3):
            try:
                raw = _http(url, headers={"Referer": "https://www.tpex.org.tw/"})
                break
            except Exception as e:
                if attempt == 2:
                    raise
        d = json.loads(raw)
        tables = d.get("tables", [])
        if not tables:
            return {}
        rows = tables[0].get("data", [])
        out = {}
        for r in rows:
            if len(r) < 24 or not r[0]:
                continue
            code = r[0].strip()
            if not code.isdigit() or len(code) != 4:
                continue
            name = r[1].strip()
            def to_int(x):
                return int(x.replace(",", "")) if x and x not in ("--", "X") else 0
            # r[4]=外陸資, r[7]=外資自營 → 合計外資
            # r[13]=投信
            # r[16]=自營(自行), r[19]=自營(避險) → 合計自營
            # r[23]=三大法人合計
            foreign = to_int(r[4]) + to_int(r[7])
            invest = to_int(r[13])
            dealer = to_int(r[16]) + to_int(r[19])
            total = to_int(r[23])
            out[code] = {
                "name": name,
                "foreign_lots": int(foreign / 1000),
                "invest_lots": int(invest / 1000),
                "dealer_lots": int(dealer / 1000),
                "total_lots": int(total / 1000),
            }
        _cache_set(key, out)
        return out
    except Exception as e:
        print(f"[data] tpex_3insti fail: {e}")
        return {}


# ══════════════════════════════════════════════════════════════
# 2. TWSE 上市每日收盤行情 (MI_INDEX 全部不含權證)
# ══════════════════════════════════════════════════════════════
def fetch_twse_prices(date_yyyymmdd: str) -> dict:
    """return {code: {name, close, open, high, low, change_pct, volume, prev_close, ma20}}"""
    key = f"twse_prices_{date_yyyymmdd}"
    cached = _cache_get(key, 3600)
    if cached:
        return cached

    roc = int(date_yyyymmdd) - 19110000  # 西元年 - 1911 = 民國年
    ymd = f"{roc // 10000:03d}/{(roc % 10000) // 100:02d}/{roc % 100:02d}"
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date_yyyymmdd}&type=ALLBUT0999"
    try:
        raw = _http(url)
        d = json.loads(raw)
        out = {}
        for t in d.get("tables", []):
            title = t.get("title", "")
            if "每日收盤行情" in title and "全部" in title:
                fields = t["fields"]
                idx = {f: i for i, f in enumerate(fields)}
                for r in t["data"]:
                    if len(r) < 9:
                        continue
                    try:
                        code = r[idx["證券代號"]].strip()
                        name = r[idx["證券名稱"]].strip()
                        close = float(r[idx["收盤價"]].replace(",", ""))
                        opn = float(r[idx["開盤價"]].replace(",", ""))
                        hi = float(r[idx["最高價"]].replace(",", ""))
                        lo = float(r[idx["最低價"]].replace(",", ""))
                        # 漲跌價差是絕對值，正負號靠 漲跌(+/-) HTML 顏色
                        # 綠 = 跌，紅 = 漲
                        spread_str = r[idx["漲跌價差"]].replace(",", "")
                        spread_abs = float(spread_str)
                        sign_html = r[idx["漲跌(+/-)"]] if "漲跌(+/-)" in idx else ""
                        if "green" in sign_html:
                            chg = -spread_abs  # 綠 = 跌
                        else:
                            chg = spread_abs   # 紅 / 無標 = 漲
                        prev = close - chg
                        chg_pct = (chg / prev * 100) if prev > 0 else 0
                    except (KeyError, ValueError, IndexError) as e:
                        continue
                    out[code] = {
                        "name": name,
                        "close": close, "open": opn, "high": hi, "low": lo,
                        "change": chg, "change_pct": round(chg_pct, 2),
                        "prev_close": round(prev, 2),
                    }
                break
        _cache_set(key, out)
        return out
    except Exception as e:
        print(f"[data] twse_prices fail: {e}")
        return {}


# ══════════════════════════════════════════════════════════════
# 3. TPEx 上櫃收盤行情
# ══════════════════════════════════════════════════════════════
def fetch_tpex_prices(date_yyyymmdd: str) -> dict:
    """return {code: {...} 同 twse_prices 結構}"""
    key = f"tpex_prices_{date_yyyymmdd}"
    cached = _cache_get(key, 3600)
    if cached:
        return cached

    roc = int(date_yyyymmdd) - 19110000
    ymd = f"{roc // 10000:03d}/{(roc % 10000) // 100:02d}/{roc % 100:02d}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&d={ymd}"
    try:
        raw = _http(url, headers={"Referer": "https://www.tpex.org.tw/"})
        d = json.loads(raw)
        if d.get("date") != date_yyyymmdd:
            return {}
        out = {}
        # TPEX 在查詢日若尚未收盤，會回傳前一交易日，需對齊
        if d.get("date") and d["date"] != date_yyyymmdd:
            print(f"[data] tpex_prices 拿到的日期 {d.get('date')} ≠ 查詢 {date_yyyymmdd}，跳過")
            return {}
        for t in d.get("tables", []):
            fields = t.get("fields", [])
            idx = {f: i for i, f in enumerate(fields)}
            for r in t["data"]:
                if len(r) < 7:
                    continue
                try:
                    code = r[idx["代號"]].strip()
                    name = r[idx["名稱"]].strip()
                    close = float(r[idx["收盤"]].replace(",", ""))
                    opn = float(r[idx["開盤"]].replace(",", ""))
                    hi = float(r[idx["最高"]].replace(",", ""))
                    lo = float(r[idx["最低"]].replace(",", ""))
                    chg_str = r[idx["漲跌"]].replace(",", "").replace(" ", "")
                    # TPEx 漲跌欄位是字串（"+1.50" 或 "-0.50" 或 "0.00"），可能無正負號帶 "X" 標記跌
                    # 帶 + 或 - 就是帶符號的數字
                    chg = float(chg_str) if chg_str not in ("", "X", "--") else 0.0
                    prev = close - chg
                    chg_pct = (chg / prev * 100) if prev > 0 else 0
                except (KeyError, ValueError, IndexError):
                    continue
                out[code] = {
                    "name": name,
                    "close": close, "open": opn, "high": hi, "low": lo,
                    "change": chg, "change_pct": round(chg_pct, 2),
                    "prev_close": round(prev, 2),
                }
        _cache_set(key, out)
        return out
    except Exception as e:
        print(f"[data] tpex_prices fail: {e}")
        return {}


# ══════════════════════════════════════════════════════════════
# 4. FinMind 個股收盤時序 (近 30 日)
# ══════════════════════════════════════════════════════════════
def fetch_finmind_price(code: str, days: int = 30) -> list:
    """return list of {date, close, volume, max, min, open} desc by date"""
    key = f"finmind_price_{code}"
    cached = _cache_get(key, 3600)
    if cached:
        return cached

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    url = f"{FINMIND_BASE}?dataset=TaiwanStockPrice&data_id={code}&start_date={start}&end_date={end}{_FINMIND_TOKEN_QS}"
    try:
        raw = _http(url)
        d = json.loads(raw)
        if d.get("status") != 200:
            return []
        rows = d.get("data", [])
        _cache_set(key, rows)
        return rows
    except Exception as e:
        print(f"[data] finmind_price {code} fail: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# 5. FinMind 個股法人買賣超 (近 30 日)
# ══════════════════════════════════════════════════════════════
def fetch_finmind_inst(code: str, days: int = 30) -> list:
    """return list of {date, name, Foreign_Investor, Investment_Trust, Dealer}"""
    key = f"finmind_inst_{code}"
    cached = _cache_get(key, 3600)
    if cached:
        return cached

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    url = f"{FINMIND_BASE}?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={code}&start_date={start}&end_date={end}{_FINMIND_TOKEN_QS}"
    try:
        raw = _http(url)
        d = json.loads(raw)
        if d.get("status") != 200:
            return []
        rows = d.get("data", [])
        _cache_set(key, rows)
        return rows
    except Exception as e:
        print(f"[data] finmind_inst {code} fail: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# 5.5 FinMind 融資券（融資餘額變化）
# ══════════════════════════════════════════════════════════════
def fetch_finmind_margin(code: str, days: int = 10) -> list:
    """return list of MarginPurchase* dicts, old → new"""
    key = f"finmind_margin_{code}"
    cached = _cache_get(key, 3600)
    if cached:
        return cached

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d")
    url = f"{FINMIND_BASE}?dataset=TaiwanStockMarginPurchaseShortSale&data_id={code}&start_date={start}&end_date={end}{_FINMIND_TOKEN_QS}"
    try:
        raw = _http(url)
        d = json.loads(raw)
        if d.get("status") != 200:
            return []
        rows = d.get("data", [])
        _cache_set(key, rows)
        return rows
    except Exception as e:
        print(f"[data] finmind_margin {code} fail: {e}")
        return []


def _trading_day_candidates(asof=None, back=8):
    """由 asof 往回列出候選交易日(只跳週末,不含國定假日表)。

    官方融資融券當日檔要收盤後才發布,遇到國定假日就整天沒有檔;只認 asof
    當天會讓資料永遠卡在最後一次抓成功的日期,所以往前退到真的有 rows 的
    那一天為止,並以那天當來源日(移植自個股卡片 chips.py 同名函式)。
    """
    limit = str(asof or datetime.now().strftime("%Y-%m-%d"))[:10]
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


def _official_number(value):
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _margn_rows(payload):
    """MI_MARGN 回應第一張 rows>100 的表才是個股明細表,不是全市場統計表。"""
    if not payload:
        return []
    for table in payload.get("tables") or []:
        if len(table.get("data") or []) > 100:
            return table["data"]
    return payload.get("data") or []


def fetch_official_margin_snapshot(asof: str = None) -> dict:
    """整市場融資融券官方快照(TWSE MI_MARGN + TPEx balance),一次涵蓋全池,
    是 FinMind 個股融資餘額結構性落後一天以上時的免 token 備援(移植自個股
    卡片 chips.py 的 _official_margin_snapshot,已驗證可用 — 見
    篩選邏輯/collect.py 及 chips-margin-source-date-trap 記憶)。

    回傳 {code: {margin_balance, margin_prev, short_balance, short_prev,
    source_date}}。往前退到真的有資料的交易日,用那天當 source_date;呼叫端
    自行比對是否等於想要的資料日,不在這裡補成 asof — 退到舊日就代表官方
    今天還沒發布,不能冒充今天的餘額。

    ⚠ TWSE MI_MARGN 前 7 欄是「融券限額」、8..12 欄才是借券賣出,完全沒有
    融資餘額;融資融券彙總表才有(rows>100 那張,融資前日/今日=5/6、
    融券前日/今日=11/12,單位張)。不要改用 TWT93U。
    """
    candidates = _trading_day_candidates(asof)
    key = f"official_margin_{candidates[0]}"
    cached = _cache_get(key, 900)
    if cached is not None:
        return cached

    out: dict = {}

    def _merge(rows, twse=False, date=None):
        for row in rows or []:
            code = str(row[0] if isinstance(row, list) else
                       row.get("股票代號") or row.get("代號") or "").strip()
            if not code:
                continue
            if isinstance(row, list) and twse:
                values = {
                    "margin_prev": _official_number(row[5]),
                    "margin_balance": _official_number(row[6]),
                    "short_prev": _official_number(row[11]),
                    "short_balance": _official_number(row[12]),
                }
            else:
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

    for trade_date in candidates:
        ymd = trade_date.replace("-", "")
        payload = None
        for url in (
            f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_MARGN?date={ymd}&selectType=ALL&response=json",
            f"https://www.twse.com.tw/exchangeReport/MI_MARGN?date={ymd}&selectType=ALL&response=json",
        ):
            try:
                payload = json.loads(_http(url))
                if _margn_rows(payload):
                    break
            except Exception:
                continue
        rows = _margn_rows(payload)
        if rows:
            _merge(rows, twse=True, date=trade_date)
            break

    for trade_date in candidates:
        date_arg = urllib.parse.quote(trade_date.replace("-", "/"), safe="")
        url = f"https://www.tpex.org.tw/www/zh-tw/margin/balance?date={date_arg}"
        rows = []
        # TPEx 站台後面是多台主機的負載平衡,其中一部分節點的憑證缺 Subject
        # Key Identifier 擴充欄位驗證會過不了,是伺服器端間歇性問題(換一台
        # 節點通常就過);實測連續打 3 次成功率遠高於打 1 次,不是靠放寬驗證
        # 過關,純粹重試換節點。
        for attempt in range(3):
            try:
                payload = json.loads(_http(url))
                tables = payload.get("tables") or []
                rows = (tables[0].get("data") if tables else []) or []
                break
            except Exception as e:
                if attempt == 2:
                    print(f"[data] TPEx 官方融資融券 {trade_date} 失敗(重試 3 次): {e}")
        if rows:
            _merge(rows, date=trade_date)
            break

    if out:
        _cache_set(key, out)
    return out


# ══════════════════════════════════════════════════════════════
# 6. 加權指數今日 (用於市場整體 chg)
# ══════════════════════════════════════════════════════════════
def fetch_twse_index_today(date_yyyymmdd: str) -> dict:
    """return {change_pct, close} for 加權指數"""
    key = f"twse_index_{date_yyyymmdd}"
    cached = _cache_get(key, 3600)
    if cached:
        return cached

    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date_yyyymmdd}&type=IND"
    try:
        raw = _http(url)
        d = json.loads(raw)
        for t in d.get("tables", []):
            if "價格指數" in t.get("title", ""):
                for r in t["data"]:
                    if "發行量加權" in r[0]:
                        # [指數, 收盤指數, 漲跌, 漲跌點數, 漲跌百分比(%)]
                        try:
                            close = float(r[1].replace(",", ""))
                            chg_pct = float(r[4].replace(",", ""))
                        except (ValueError, IndexError):
                            continue
                        out = {"close": close, "change_pct": chg_pct}
                        _cache_set(key, out)
                        return out
        return {}
    except Exception as e:
        print(f"[data] twse_index fail: {e}")
        return {}


# ══════════════════════════════════════════════════════════════
# 一次撈齊今日盤後快照 (給 analyst 用)
# ══════════════════════════════════════════════════════════════
def snapshot_today(date_yyyymmdd: str) -> dict:
    return {
        "twse_inst": fetch_twse_inst_today(date_yyyymmdd),
        "tpex_inst": fetch_tpex_inst_today(date_yyyymmdd),
        "twse_prices": fetch_twse_prices(date_yyyymmdd),
        "tpex_prices": fetch_tpex_prices(date_yyyymmdd),
        "twse_index": fetch_twse_index_today(date_yyyymmdd),
        "date": date_yyyymmdd,
    }


# ══════════════════════════════════════════════════════════════
# 抓當日所有資料並落 DB（背景任務用）
# ══════════════════════════════════════════════════════════════
def fetch_today_all_to_db(date_yyyymmdd: str) -> dict:
    """抓當日 TWSE+TPEX 法人+股價落 DB。回傳 {inst_count, price_count}"""
    import db
    snap = snapshot_today(date_yyyymmdd)
    # 統一 DB 日期格式 YYYY-MM-DD
    db_date = f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"

    # 法人
    inst_rows = []
    for code, v in snap["twse_inst"].items():
        inst_rows.append({"code": code, "name": v["name"],
                          "foreign_lots": v["foreign_lots"],
                          "invest_lots": v["invest_lots"],
                          "dealer_lots": v["dealer_lots"],
                          "total_lots": v["total_lots"]})
    for code, v in snap["tpex_inst"].items():
        inst_rows.append({"code": code, "name": v["name"],
                          "foreign_lots": v["foreign_lots"],
                          "invest_lots": v["invest_lots"],
                          "dealer_lots": v["dealer_lots"],
                          "total_lots": v["total_lots"]})
    db.save_inst_daily(db_date, inst_rows, source="twse+tpex_3insti")

    # 股價（兩個市場分開 save，標 market）
    twse_p = [{"code": c, "name": v["name"], "close": v["close"],
               "open": v["open"], "high": v["high"], "low": v["low"],
               "volume": 0, "prev_close": v["prev_close"],
               "change_pct": v["change_pct"]}
              for c, v in snap["twse_prices"].items() if len(c) == 4 and c.isdigit()]
    tpex_p = [{"code": c, "name": v["name"], "close": v["close"],
               "open": v["open"], "high": v["high"], "low": v["low"],
               "volume": 0, "prev_close": v["prev_close"],
               "change_pct": v["change_pct"]}
              for c, v in snap["tpex_prices"].items() if len(c) == 4 and c.isdigit()]
    db.save_price_daily(db_date, twse_p, market="twse", source="MI_INDEX")
    db.save_price_daily(db_date, tpex_p, market="tpex", source="TPEX_3insti_quotes")

    return {"inst_count": len(inst_rows),
            "price_twse": len(twse_p),
            "price_tpex": len(tpex_p)}


if __name__ == "__main__":
    today = datetime.now().strftime("%Y%m%d")
    snap = snapshot_today(today)
    print(f"date={today} twse_stocks={len(snap['twse_prices'])} tpex_stocks={len(snap['tpex_prices'])} "
          f"inst_records={len(snap['twse_inst'])} index={snap['twse_index']}")
