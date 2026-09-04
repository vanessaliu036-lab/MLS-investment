"""
MLS 模組 — eod_source.py(v4 新增,盤後資料源)
====================================================================
問題:盤後(收盤後)Shioaji 即時快照回 0 筆,首頁的資金健康度/漏斗/熱力圖
全部空白。使用者定案:盤後可用 FinMind。

本模組用 FinMind TaiwanStockPrice(免費、日更)組出「收盤快照」,欄位對齊
broker.batch_snapshots 的輸出,讓 engine.compute_sector_flow / money_health.annotate
/ funnel 在盤後也能吃到真資料。

誠實邊界:
  - 盤中的「主動買賣量(aflow)」EOD 無法取得 → 資金腳(flow)在盤後為中性,
    money_health 象限退化為「漲=in_up / 跌=in_down」。前端會標「盤後·EOD」。
  - 抓不到就略過該檔,不假造。
  - 當日快取(記憶體),避免每次刷新都打 FinMind(免費層 300/hr)。
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TW_TZ = timezone(timedelta(hours=8))
_FINMIND = "https://api.finmindtrade.com/api/v4/data"
_TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()

_cache = {}          # {trade_date: [snaps]}


def _today():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "MLS/4 eod"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _official_month_rows(code, trade_date=None):
    """TWSE/TPEx 官方近月 OHLC,供所有 EOD 消費者共用。"""
    now = datetime.now(TW_TZ)
    target = datetime.strptime(trade_date, "%Y-%m-%d") if trade_date else now
    ymd = target.strftime("%Y%m%d")
    roc = f"{target.year - 1911:03d}/{target.month:02d}/{target.day:02d}"
    month_start = target.replace(day=1).strftime("%Y/%m/%d")

    def num(value):
        try:
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    def date_text(value):
        p = str(value).split("/")
        if len(p) == 3 and p[0].isdigit() and int(p[0]) < 1000:
            return f"{int(p[0]) + 1911:04d}-{int(p[1]):02d}-{int(p[2]):02d}"
        return str(value)

    urls = [
        ("twse", "https://www.twse.com.tw/exchangeReport/STOCK_DAY?" +
         urllib.parse.urlencode({"response": "json", "date": ymd,
                                 "stockNo": str(code)})),
        # TPEx 的新版 tradingStock API 使用西元 month-start 的 `date`；
        # `d` 是舊頁面參數，帶 `d` 會被忽略並固定回目前月份，導致歷史圖只剩 1–2 根。
        ("tpex", "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?" +
         urllib.parse.urlencode({"response": "json", "date": month_start,
                                 "code": str(code)})),
    ]
    for source, url in urls:
        try:
            payload = _get(url, timeout=15)
            rows = payload.get("data", []) if source == "twse" else (
                (payload.get("tables") or [{}])[0].get("data", []))
            out = []
            for row in rows:
                if len(row) < 7:
                    continue
                high, low, close = num(row[4]), num(row[5]), num(row[6])
                if high is None or low is None or close is None:
                    continue
                volume = num(row[1]) or 0
                amount = num(row[2]) or 0
                if source == "tpex":
                    volume *= 1000       # 櫃買回傳成交張數
                    amount *= 1000       # 櫃買回傳成交仟元
                out.append({"date": date_text(row[0]), "close": close,
                            "max": high, "min": low,
                            "change": num(row[7]) if len(row) > 7 else None,
                            "Trading_Volume": volume,
                            "Trading_money": amount,
                            "source": f"official_{source}"})
            if out:
                return out
        except Exception as e:
            print(f"[eod] {source} 官方月K {code} 失敗:{e}")
    return []


def _price_rows(code, start_date, trade_date=None):
    # 收盤價的第一權威來源必須是交易所成品；FinMind 只能做斷線備援。
    # 舊邏輯先問 FinMind，造成「API 成功但不是今日官方價」也被當成正確資料。
    official = _official_month_rows(code, trade_date=trade_date)
    if official:
        return official

    url = (f"{_FINMIND}?dataset=TaiwanStockPrice&data_id={code}"
           f"&start_date={start_date}")
    if _TOKEN:
        url += f"&token={_TOKEN}"
    try:
        j = _get(url)
        if j.get("status") == 200 and j.get("data"):
            rows = j.get("data", [])
            for row in rows:
                row.setdefault("source", "finmind_fallback")
            return rows
    except Exception:
        pass
    return []


def _snap_from_rows(code, rows, name_map, sector_map, trade_date=None):
    """rows 為 FinMind TaiwanStockPrice(日期升冪)。組單檔收盤快照。"""
    rows = [r for r in rows if r.get("close")]
    if trade_date:
        rows = [r for r in rows if str(r.get("date", "")) <= trade_date]
    if len(rows) < 2:
        return None
    today, prev = rows[-1], rows[-2]
    close = float(today["close"])
    prevc = float(prev["close"]) or close
    chg = (close - prevc) / prevc * 100 if prevc else 0.0
    vol = float(today.get("Trading_Volume") or 0)          # 股
    hist = [float(r.get("Trading_Volume") or 0) for r in rows[-6:-1]]
    avg5 = (sum(hist) / len(hist)) if hist else 0
    vr = round(vol / avg5, 2) if avg5 else 1.0
    high = float(today.get("max") or close)
    low = float(today.get("min") or close)
    amount = float(today.get("Trading_money") or close * vol)
    sec, stype = sector_map.get(code, (None, None))
    return {
        "code": code,
        "name": name_map.get(code, code),
        "price": round(close, 2),
        "change_rate": round(chg, 2),
        "total_volume": int(vol / 1000),                    # 張
        "total_amount": amount,
        "volume_ratio": vr,
        "high": high,
        "low": low,
        "avg_price": round((high + low + close) / 3, 2),     # 典型價當均價代理
        "sector": sec, "sector_name": sec, "sector_type": stype,
        "eod": True,                                         # 標記盤後 EOD 來源
        "source": today.get("source") or "unknown",
        "source_date": today.get("date"),
    }


def _persist_snaps(tdate, snaps):
    """抓成功就落地 eod_snapshot 表(一天只需抓一次,重啟不掉、不再爆額度)。"""
    try:
        import db
        with db._lock, db._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS eod_snapshot(
              trade_date TEXT, code TEXT, payload TEXT,
              PRIMARY KEY(trade_date, code))""")
            for s in snaps:
                c.execute("INSERT OR REPLACE INTO eod_snapshot VALUES(?,?,?)",
                          (tdate, s["code"], json.dumps(s, ensure_ascii=False)))
    except Exception as e:
        print(f"[eod] 落地失敗:{e}")


def _load_snaps_db(tdate=None):
    """FinMind 失敗時,讀最近一次落地的收盤快照。"""
    try:
        import db
        with db._lock, db._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS eod_snapshot(
              trade_date TEXT, code TEXT, payload TEXT,
              PRIMARY KEY(trade_date, code))""")
            if tdate is None:
                r = c.execute("SELECT MAX(trade_date) d FROM eod_snapshot").fetchone()
                tdate = r["d"] if r else None
            if not tdate:
                return []
            rows = c.execute("SELECT payload FROM eod_snapshot WHERE trade_date=?",
                             (tdate,)).fetchall()
        return [json.loads(r["payload"]) for r in rows]
    except Exception:
        return []


def _from_livermore():
    """最後備援:用 livermore_record 已存的收盤價組最小快照(缺量→中性)。"""
    try:
        import db, config as C
        with db._lock, db._conn() as c:
            rows = c.execute("""SELECT code,name,sector,price,high,low FROM livermore_record
                WHERE trade_date=(SELECT MAX(trade_date) FROM livermore_record)""").fetchall()
        out = []
        for r in rows:
            cl = r["price"]
            if cl is None:
                continue
            sec, stype = C.SECTOR_MAP.get(r["code"], (r["sector"], "attack"))
            out.append({"code": r["code"], "name": r["name"] or C.NAME_MAP.get(r["code"], r["code"]),
                        "price": cl, "change_rate": 0.0, "total_volume": 0,
                        "total_amount": 0.0, "volume_ratio": 1.0,
                        "high": r["high"] or cl, "low": r["low"] or cl,
                        "avg_price": cl, "sector": sec, "sector_name": sec,
                        "sector_type": stype, "eod": True, "degraded": True})
        return out
    except Exception:
        return []


def eod_snaps(codes=None, trade_date=None):
    """回傳收盤快照列表(對齊 batch_snapshots 欄位)。
    優先序:記憶體快取 → FinMind(成功即落地DB)→ DB 最近一次 → livermore_record 墊底。"""
    import config as C
    tdate = trade_date or _today()
    if tdate in _cache:
        cached = _cache[tdate]
        if codes is None:
            return cached
        wanted = set(codes)
        return [s for s in cached if s.get("code") in wanted]
    codes = codes or list(C.UNIVERSE)
    start = (datetime.now(TW_TZ) - timedelta(days=25)).strftime("%Y-%m-%d")
    out = []
    fails = 0
    for i, code in enumerate(codes):
        rows = _price_rows(code, start, trade_date=tdate)
        snap = (_snap_from_rows(code, rows, C.NAME_MAP, C.SECTOR_MAP,
                                trade_date=tdate) if rows else None)
        if snap:
            out.append(snap); fails = 0
        else:
            fails += 1
            if fails >= 3 and not out:      # FinMind 連掛(限流/休市)→ 別再空跑,直接走備援
                break
        if i % 10 == 9:
            time.sleep(0.3)
    if out and len(out) >= max(5, len(codes) // 2):
        _cache[tdate] = out
        _persist_snaps(tdate, out)
        return out
    # FinMind 抓不足 → 讀 DB 最近一次;再不行用 livermore 墊底(都快,單次 DB 查詢)
    fb = _load_snaps_db(tdate) or _load_snaps_db() or _from_livermore()
    if fb:
        _cache[tdate] = fb                  # 快取備援,後續輪詢秒回
    return fb or out


def eod_state(trade_date=None):
    """組一個 last_state 樣子的 dict(給 money_health / funnel 盤後用)。
    回傳 {_snaps, _sectors_full, sectors, market_pct, eod}。"""
    import engine
    import official_source
    snaps = eod_snaps(trade_date=trade_date)
    sectors = engine.compute_sector_flow(snaps) if snaps else []
    idx = {}
    try:
        idx = official_source.market_index() or {}
    except Exception:
        pass
    return {"_snaps": snaps, "_sectors_full": sectors, "sectors": sectors,
            "market_pct": idx.get("change_pct") or 0.0, "eod": True}
