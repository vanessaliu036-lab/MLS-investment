"""
MLS 獨立插件 — livermore.py
李佛摩價格紀錄法 · 六欄色表 + 每日持久化歷史 v2.0
====================================================================
【完全獨立】此插件不修改你現有任何檔案。
它自帶:
  • 六欄色表狀態機(李佛摩原書手寫簿還原)
  • 自己的 mls.db 資料表(CREATE TABLE IF NOT EXISTS,不碰舊表)
  • 自己的 FastAPI router(掛載只需在 server.py 加「一行」)
  • 每日盤後把固定觀察池 50 檔的六欄紀錄各存一列,累積成歷史

────────────────────────────────────────────────────────────────
掛載方式(server.py 只加一行,不改任何舊程式):
    import livermore
    app.include_router(livermore.router)     # ← 就這一行

前端 livermore.html 直接呼叫本 router 提供的 API:
    GET  /livermore                 → 回傳前端頁面(也可自行以靜態檔開)
    GET  /api/liv/record?code=2337  → 單檔六欄歷史(縱向一天一列)
    GET  /api/liv/overview          → 全觀察池最新狀態總覽列表
    POST /api/liv/snapshot          → 立即抓價、寫入今日六欄紀錄(手動觸發)
    GET  /api/liv/dates             → 已存檔日期清單

每日自動存檔:於 server.py 盤後(或 after_hours)呼叫
    livermore.record_today()
即可。不呼叫也能用「手動 snapshot」按鈕即時補寫。

────────────────────────────────────────────────────────────────
李佛摩六欄(六色)定義(忠實原書):
  次級反彈 SEC_RALLY   / 自然反彈 NAT_RALLY
  上升趨勢 UPTREND     / 下降趨勢 DOWNTREND
  自然回檔 NAT_REACT   / 次級回檔 SEC_REACT
每日該檔價格只會落在其中「一欄」,其餘欄留白 —— 這就是他那張簿子。
關鍵點(轉向點)以粗體/標記呈現:突破前一趨勢極值 = Pivotal Point。
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

try:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse, HTMLResponse
    _HAS_FASTAPI = True
except Exception:                     # 測試環境無 fastapi 時仍可用引擎/DB
    _HAS_FASTAPI = False

    class APIRouter:                  # 極簡替身,讓裝飾器不炸
        def get(self, *a, **k):
            return lambda fn: fn

        def post(self, *a, **k):
            return lambda fn: fn

    def JSONResponse(x, **k):
        return x

    def HTMLResponse(x, **k):
        return x

# ── 與主系統共用設定(只讀,不改) ──
import config as C
try:
    import broker
except Exception:
    broker = None

TW_TZ = timezone(timedelta(hours=8))
DB_PATH = os.path.join(os.path.dirname(__file__), "mls.db")
HTML_PATH = os.path.join(os.path.dirname(__file__), "livermore.html")
_lock = threading.Lock()

# ── 可調參數(集中此處) ──
PIVOT_SWING_PCT = 6.0     # 主要狀態切換門檻(%);李佛摩約六點擺動
SEC_SWING_PCT = 3.0       # 次級波動門檻(%)
KBAR_DAYS = 90            # 建表回看日數

# 六欄
SEC_RALLY = "次級反彈"
NAT_RALLY = "自然反彈"
UPTREND = "上升趨勢"
DOWNTREND = "下降趨勢"
NAT_REACT = "自然回檔"
SEC_REACT = "次級回檔"

# 欄位順序(前端由左至右;對應李佛摩原書欄序)
COLUMNS = [SEC_RALLY, NAT_RALLY, UPTREND, DOWNTREND, NAT_REACT, SEC_REACT]

# 六色(台股慣例:紅漲綠跌;上升系偏紅,下降系偏綠,次級偏中性金)
COLCOLOR = {
    SEC_RALLY: "#c99a1e", NAT_RALLY: "#e0662b",
    UPTREND: "#c0342c", DOWNTREND: "#1a8a5a",
    NAT_REACT: "#2f6bd0", SEC_REACT: "#6a4bb0",
}


# ════════════════════════════════════════════════════════
# 一、資料層(自建表,不碰舊表)
# ════════════════════════════════════════════════════════
def init_db():
    """建立李佛摩專用表;IF NOT EXISTS,對舊庫零影響。"""
    with _lock, sqlite3.connect(DB_PATH) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS livermore_record(
          trade_date TEXT,
          code       TEXT,
          name       TEXT,
          sector     TEXT,
          stock_type TEXT,          -- engine / attack
          price      REAL,          -- 當日收盤(或快照價)
          high       REAL,
          low        REAL,
          state      TEXT,          -- 六欄之一
          pivot      TEXT,          -- 關鍵點種類(無則空)
          pivot_price REAL,
          PRIMARY KEY(trade_date, code)
        );
        CREATE INDEX IF NOT EXISTS idx_liv_code ON livermore_record(code, trade_date);
        """)


def _save_rows(trade_date, rows):
    with _lock, sqlite3.connect(DB_PATH) as c:
        c.executemany("""
          INSERT OR REPLACE INTO livermore_record
          (trade_date, code, name, sector, stock_type,
           price, high, low, state, pivot, pivot_price)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, rows)


def _fetch_code(code, limit=120):
    with _lock, sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rs = c.execute("""SELECT * FROM livermore_record
            WHERE code=? ORDER BY trade_date ASC LIMIT ?""",
            (code, limit)).fetchall()
    return [dict(r) for r in rs]


def _fetch_latest_all():
    with _lock, sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rs = c.execute("""
          SELECT r.* FROM livermore_record r
          JOIN (SELECT code, MAX(trade_date) md
                FROM livermore_record GROUP BY code) m
          ON r.code=m.code AND r.trade_date=m.md
        """).fetchall()
    return [dict(r) for r in rs]


def _fetch_dates():
    with _lock, sqlite3.connect(DB_PATH) as c:
        rs = c.execute("""SELECT DISTINCT trade_date
            FROM livermore_record ORDER BY trade_date DESC""").fetchall()
    return [r[0] for r in rs]


# ════════════════════════════════════════════════════════
# 二、李佛摩六欄狀態機(單檔;沿用已驗證正確的邏輯)
# ════════════════════════════════════════════════════════
class LivermoreRecord:
    def __init__(self, code, name, bars):
        self.code, self.name, self.bars = code, name, bars or []
        self.state = None
        self.pivot_up = None
        self.pivot_down = None
        self.last_nat_rally_high = None
        self.last_nat_react_low = None
        self.trend_high = None
        self.trend_low = None
        self.pivots = []
        self.history = []      # 每日 {date,state,price,high,low,color,pivot}
        self._build()

    @staticmethod
    def _pct(a, b):
        return 0.0 if not b else (a - b) / b * 100.0

    def _build(self):
        for bar in self.bars:
            hi, lo = bar.get("high"), bar.get("low")
            cl = bar.get("close", hi)
            if hi is None or lo is None:
                continue
            self._step(bar.get("date"), hi, lo, cl)

    def _step(self, date, hi, lo, cl):
        pivot_here = None
        if self.state is None:
            self.state = UPTREND
            self.trend_high, self.trend_low = hi, lo
            self.pivot_up = hi
            self._log(date, cl, hi, lo, None)
            return

        if self.state in (UPTREND, SEC_RALLY, NAT_RALLY):
            if hi >= (self.trend_high or hi):
                prev_high = self.trend_high
                self.trend_high = hi
                in_downtrend_bounce = (
                    self.pivot_down is not None and
                    (self.pivot_up is None or hi <= self.pivot_up))
                if in_downtrend_bounce:
                    self.state = SEC_RALLY if self.state == SEC_RALLY else NAT_RALLY
                else:
                    if self.state in (NAT_RALLY, SEC_RALLY):
                        if (self.last_nat_react_low is None or
                                lo > self.last_nat_react_low):
                            pivot_here = "多方續勢關鍵點"
                    if self.pivot_up and hi > self.pivot_up * (1 + 1e-9):
                        if prev_high is not None and prev_high < self.pivot_up:
                            pivot_here = "多方突破關鍵點"
                        self.pivot_up = hi
                        self.pivot_down = None
                    self.state = UPTREND
                    self.trend_low = lo
            else:
                drop = -self._pct(cl, self.trend_high)
                if drop >= PIVOT_SWING_PCT:
                    self.pivot_up = self.trend_high
                    self.last_nat_rally_high = self.trend_high
                    self.state = NAT_REACT
                    self.trend_low = lo
                    self.last_nat_react_low = lo
                    if self.pivot_down is None:
                        self.pivot_down = lo
                elif drop >= SEC_SWING_PCT:
                    self.state = SEC_REACT
                    self.trend_low = lo

        elif self.state in (DOWNTREND, SEC_REACT, NAT_REACT):
            if lo <= (self.trend_low or lo):
                prev_low = self.trend_low
                self.trend_low = lo
                if (self.state in (NAT_REACT, SEC_REACT)
                        and self.pivot_down is not None):
                    if (self.last_nat_rally_high is None or
                            hi < self.last_nat_rally_high):
                        pivot_here = "空方續勢關鍵點"
                if self.pivot_down is None:
                    self.pivot_down = lo
                elif lo < self.pivot_down * (1 - 1e-9):
                    if prev_low is not None and prev_low > self.pivot_down:
                        pivot_here = "空方跌破關鍵點"
                    self.pivot_down = lo
                self.state = DOWNTREND
                self.trend_high = hi
            else:
                rise = self._pct(cl, self.trend_low)
                if rise >= PIVOT_SWING_PCT:
                    self.pivot_down = self.trend_low
                    self.last_nat_react_low = self.trend_low
                    self.state = NAT_RALLY
                    self.trend_high = hi
                    self.last_nat_rally_high = hi
                elif rise >= SEC_SWING_PCT:
                    self.state = SEC_RALLY
                    self.trend_high = hi

        if pivot_here:
            self.pivots.append({"date": str(date), "price": round(cl, 2),
                                "kind": pivot_here})
        self._log(date, cl, hi, lo, pivot_here)

    def _log(self, date, price, hi, lo, pivot):
        self.history.append({
            "date": str(date), "state": self.state,
            "price": round(price or 0, 2),
            "high": round(hi or 0, 2), "low": round(lo or 0, 2),
            "color": COLCOLOR.get(self.state, "#15181e"),
            "pivot": pivot,
        })

    def latest(self):
        return self.history[-1] if self.history else None


# ════════════════════════════════════════════════════════
# 三、日K 取得 + 建單檔紀錄
# ════════════════════════════════════════════════════════
def _get_bars(code, days=KBAR_DAYS, injected=None):
    if injected is not None:
        return injected
    if broker is None:
        return []
    try:
        raw = broker.daily_kbars(code, days=days)
    except Exception as e:
        print(f"[livermore] {code} 日K失敗:{e}")
        return []
    bars = []
    for r in raw:
        hi, cl = r.get("high"), r.get("close")
        lo = r.get("low", cl if cl is not None else hi)
        bars.append({"date": r.get("date"), "high": hi, "low": lo, "close": cl})
    return bars


def build_record(code, injected=None):
    name = C.NAME_MAP.get(code, code)
    rec = LivermoreRecord(code, name, _get_bars(code, injected=injected))
    return rec


# ════════════════════════════════════════════════════════
# 四、每日存檔(盤後呼叫 record_today();或前端手動 snapshot)
# ════════════════════════════════════════════════════════
def record_today(codes=None, injected_map=None):
    """
    對固定觀察池每檔算六欄紀錄,把「今日該檔狀態」存為一列。
    codes: 預設 config.UNIVERSE 全部;injected_map 供測試。
    回傳 {date, saved, rows}。
    """
    init_db()
    codes = codes or C.UNIVERSE
    trade_date = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    rows, preview = [], []
    for code in codes:
        inj = (injected_map or {}).get(code)
        rec = build_record(code, injected=inj)
        last = rec.latest()
        if not last:
            continue
        sector, typ = C.SECTOR_MAP.get(code, ("—", "attack"))
        if code in C.ENGINE_STOCKS:
            typ = "engine"
        rows.append((trade_date, code, rec.name, sector, typ,
                     last["price"], last["high"], last["low"],
                     last["state"], last["pivot"] or "",
                     rec.pivots[-1]["price"] if rec.pivots else None))
        preview.append({"code": code, "name": rec.name,
                        "state": last["state"], "pivot": last["pivot"]})
    if rows:
        _save_rows(trade_date, rows)
    return {"date": trade_date, "saved": len(rows), "rows": preview}


# ════════════════════════════════════════════════════════
# 五、FastAPI Router(掛載一行:app.include_router(livermore.router))
# ════════════════════════════════════════════════════════
router = APIRouter()


@router.get("/api/liv/record")
def api_record(code: str):
    """
    單檔六欄歷史。以日K即時重算完整序列(90日),呈現如李佛摩整本簿;
    DB 存的每日官方紀錄僅作為累積存證,不限制詳細表的顯示深度。
    """
    rec = build_record(code)
    return JSONResponse({"code": code, "name": rec.name,
                         "columns": COLUMNS, "colors": COLCOLOR,
                         "rows": rec.history})


@router.get("/api/liv/overview")
def api_overview():
    """全觀察池最新狀態總覽(DB 優先;空庫則即時算一輪)。"""
    latest = _fetch_latest_all()
    if not latest:
        out = []
        for code in C.UNIVERSE:
            rec = build_record(code)
            last = rec.latest()
            if not last:
                continue
            sector, typ = C.SECTOR_MAP.get(code, ("—", "attack"))
            if code in C.ENGINE_STOCKS:
                typ = "engine"
            out.append({"code": code, "name": rec.name, "sector": sector,
                        "stock_type": typ, "state": last["state"],
                        "price": last["price"], "pivot": last["pivot"],
                        "color": last["color"]})
        latest_rows = out
    else:
        latest_rows = [{"code": r["code"], "name": r["name"],
                        "sector": r["sector"], "stock_type": r["stock_type"],
                        "state": r["state"], "price": r["price"],
                        "pivot": r["pivot"] or None,
                        "color": COLCOLOR.get(r["state"], "#15181e")}
                       for r in latest]
    latest_rows.sort(key=lambda x: (x["stock_type"] != "attack", x["sector"]))
    return JSONResponse({"columns": COLUMNS, "colors": COLCOLOR,
                         "stocks": latest_rows})


@router.post("/api/liv/snapshot")
def api_snapshot():
    """手動觸發:立刻抓價寫入今日六欄紀錄。"""
    try:
        res = record_today()
        return JSONResponse({"ok": True, **res})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/api/liv/dates")
def api_dates():
    return JSONResponse({"dates": _fetch_dates()})


@router.get("/livermore", response_class=HTMLResponse)
def page():
    try:
        with open(HTML_PATH, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h3>livermore.html 未放入資料夾</h3>", status_code=404)



# ════════════════════════════════════════════════════════
# v2.4.1 新增:六點轉向判定層(每日盤後選股中心)
# ════════════════════════════════════════════════════════
# 系統定位:盤後才能確定的六點——
#   ① 是否突破 60 日高   ② 是否跌破 60 日低   ③ 今日成交量是否放大
#   ④ 是否回測 50% 未破  ⑤ 是否形成轉向點(六欄關鍵點)
#   ⑥ 是否正式進入觀察池(合格)
# 不動上方六欄狀態機;本層為獨立 Scanner,收盤後計算一次。
# 合格定義(可調):
#   多方合格 = 突破60日高 且 量>5日均×VOL_X(突破本身即李佛摩式訊號)
#   空方合格 = 跌破60日低 且 量>5日均×VOL_X
#   六欄狀態/關鍵點/50%回測為記錄旗標,交由決策中心(decision_v22)加分,
#   不作為合格必要件——避免盤整突破被狀態機延遲確認而漏抓
# 引擎股(config.ENGINE_STOCKS)照鐵律永不列合格。

SIX_LOOKBACK = 60      # 高低點回看天數
VOL_X = 1.5            # 量放大倍數(今日量 > 5日均量 × VOL_X)
RETEST_WIN = 20        # 50% 回測判定的波段視窗


def init_sixpoint_db():
    with _lock, sqlite3.connect(DB_PATH) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS livermore_sixpoint(
          trade_date TEXT, code TEXT,
          break60_high INTEGER, break60_low INTEGER,
          vol_expand INTEGER, vol_ratio REAL,
          retest50_hold INTEGER, pivot TEXT, state TEXT,
          qualified TEXT,               -- 'long' / 'short' / NULL
          close REAL, hi60 REAL, lo60 REAL,
          PRIMARY KEY(trade_date, code)
        );""")


def six_point_eval(bars, state=None, pivot=None):
    """單檔六點判定。bars: 舊→新 list[{date,close,high,low,volume}]。
    回傳 dict;資料不足回 None。"""
    if not bars or len(bars) < SIX_LOOKBACK + 6:
        return None
    today = bars[-1]
    prior = bars[-(SIX_LOOKBACK + 1):-1]
    hi60 = max((b.get("high") or b["close"]) for b in prior)
    lo60 = min((b.get("low") or b["close"]) for b in prior)
    cl = today["close"]
    break_high = 1 if cl > hi60 else 0
    break_low = 1 if cl < lo60 else 0
    vols = [b.get("volume") or 0 for b in bars[-6:-1]]
    avg5 = sum(vols) / 5 if len(vols) == 5 and sum(vols) else None
    vr = round((today.get("volume") or 0) / avg5, 2) if avg5 else None
    vol_expand = 1 if (vr is not None and vr >= VOL_X) else 0
    # 50% 回測:近 RETEST_WIN 日波段中點之上收盤 = 回測未破
    seg = bars[-RETEST_WIN:]
    hi_w = max((b.get("high") or b["close"]) for b in seg)
    lo_w = min((b.get("low") or b["close"]) for b in seg)
    mid = (hi_w + lo_w) / 2
    retest_hold = 1 if cl >= mid else 0
    qualified = None
    if break_high and vol_expand:      # 突破60日高+量放大=正式進觀察池
        qualified = "long"
    elif break_low and vol_expand:
        qualified = "short"
    return {"break60_high": break_high, "break60_low": break_low,
            "vol_expand": vol_expand, "vol_ratio": vr,
            "retest50_hold": retest_hold, "pivot": pivot, "state": state,
            "qualified": qualified, "close": cl,
            "hi60": round(hi60, 2), "lo60": round(lo60, 2)}


def six_point_scan(codes=None, injected_map=None):
    """全池六點掃描並落地。回傳 {date, scanned, qualified:[...]}"""
    init_sixpoint_db()
    codes = codes or C.UNIVERSE
    tdate = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    qualified = []
    n = 0
    for code in codes:
        bars = _get_bars(code, injected=(injected_map or {}).get(code))
        if not bars:
            continue
        rec = None
        try:
            rec = build_record(code, injected=(injected_map or {}).get(code))
        except Exception:
            pass
        last = rec.latest() if rec else None
        state = last["state"] if last else None
        pivot = last.get("pivot") if last else None
        ev = six_point_eval(bars, state=state, pivot=pivot)
        if ev is None:
            continue
        # v3.0:引擎股可合格(雙軌玩法,引擎=波段軌,不再排除)
        n += 1
        with _lock, sqlite3.connect(DB_PATH) as c:
            c.execute("""INSERT OR REPLACE INTO livermore_sixpoint VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (tdate, code, ev["break60_high"], ev["break60_low"],
               ev["vol_expand"], ev["vol_ratio"], ev["retest50_hold"],
               ev["pivot"], ev["state"], ev["qualified"],
               ev["close"], ev["hi60"], ev["lo60"]))
        if ev["qualified"]:
            qualified.append({"code": code,
                              "name": C.NAME_MAP.get(code, code), **ev})
    return {"date": tdate, "scanned": n, "qualified": qualified}


@router.get("/api/liv/sixpoint")
def api_sixpoint(date: str = ""):
    init_sixpoint_db()
    with _lock, sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        if not date:
            r = c.execute("SELECT MAX(trade_date) d FROM livermore_sixpoint").fetchone()
            date = (r["d"] if r else None) or ""
        rows = [dict(x) for x in c.execute(
            """SELECT * FROM livermore_sixpoint WHERE trade_date=?
               ORDER BY (qualified IS NULL), break60_high DESC, vol_ratio DESC""",
            (date,))] if date else []
    return {"date": date, "rows": rows,
            "qualified": [r for r in rows if r["qualified"]]}


@router.post("/api/liv/sixpoint_scan")
def api_sixpoint_scan():
    try:
        out = six_point_scan()
        return {"ok": True, **{k: out[k] for k in ("date", "scanned")},
                "qualified": len(out["qualified"])}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# 獨立冒煙測試
if __name__ == "__main__":
    seqA = [100,104,108,112,116,120, 116,112,108,112, 118,122,126]
    seqB = [100,96,92,88,84,80, 84,88,84, 78,74,70]
    def mk(seq):
        return [{"date": f"2026-06-{i+1:02d}", "high": p*1.01,
                 "low": p*0.99, "close": p} for i, p in enumerate(seq)]
    inj = {"2337": mk(seqA), "2344": mk(seqB)}
    r = record_today(codes=["2337", "2344"], injected_map=inj)
    print("存檔:", r)
    for code in ("2337", "2344"):
        rec = build_record(code, injected=inj[code])
        print(f"\n{code} {rec.name} 末列:", rec.latest()["state"],
              "| 關鍵點:", [p["kind"] for p in rec.pivots])

    # 六點轉向層驗證:66根盤整後末日放量突破60日高
    base = [{"date": f"D{i:03d}", "close": 100 + (i % 7) * 0.5,
             "high": 101 + (i % 7) * 0.5, "low": 99 + (i % 7) * 0.5,
             "volume": 5000} for i in range(66)]
    base += [{"date": "D066", "close": 108, "high": 108.5, "low": 104,
              "volume": 12000}]   # 放量突破(hi60≈104.5)
    ev = six_point_eval(base, state="上升趨勢", pivot="多方突破關鍵點")
    print("\n六點判定:", ev)
    assert ev["break60_high"] == 1 and ev["vol_expand"] == 1
    assert ev["qualified"] == "long" and ev["retest50_hold"] == 1
    out = six_point_scan(codes=["2337"], injected_map={"2337": base})
    print("六點掃描落地:", out["date"], "合格", len(out["qualified"]), "檔")
    assert len(out["qualified"]) == 1
