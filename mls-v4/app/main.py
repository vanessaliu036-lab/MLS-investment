"""
MLS v4.0 — main.py
FastAPI 主程式 + 所有 API 路由 + 排程 + 前端。

啟動即：建表 → 連 broker（失敗降級 demo）→ 若無今日資料先跑一次 EOD → 啟動排程。
系統啟動當下就能開網頁看到畫面（demo 或真實），接上金鑰後自動切真實資料。
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import config as C
import db
import broker
import after_hours
import livermore
import report
import decision
import history

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


def _bootstrap():
    """啟動初始化：建表、連線、確保有當日資料。"""
    db.init()
    broker.connect()
    print(f"[MLS] DATA_MODE={C.DATA_MODE} broker_connected={broker.is_connected()}")
    # 若今日尚無盤後資料，先跑一次（demo 模式立即有畫面）
    today = db.today()
    if not db.load_dec_health(today):
        try:
            after_hours.run_eod(today)
            report.daily_report(today)
            print(f"[MLS] 已生成 {today} 初始盤後資料")
        except Exception as e:
            print(f"[MLS] 初始 EOD 失敗（不影響啟動）：{e}")
    else:
        # 即使 dec_health 已有,仍確保今日 inst_daily 已寫入(給 chips.get_chips 當日 20 日合計用)
        try:
            ymd = today.replace("-", "")
            import data_collector
            r = data_collector.fetch_today_all_to_db(ymd)
            print(f"[MLS] 已補抓今日法人 inst_count={r.get('inst_count')}")
        except Exception as e:
            print(f"[MLS] ⚠️ 補抓今日法人失敗:{e}")


def _setup_scheduler(app):
    """APScheduler：每交易日 15:05 跑盤後。"""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        sched = BackgroundScheduler(timezone="Asia/Taipei")

        def eod_job():
            try:
                s = after_hours.run_eod()
                report.daily_report()
                print(f"[MLS] 排程 EOD 完成：{s}")
            except Exception as e:
                print(f"[MLS] 排程 EOD 失敗：{e}")

        sched.add_job(eod_job, CronTrigger(day_of_week="mon-fri", hour=15, minute=5))
        sched.start()
        app.state.sched = sched
        print("[MLS] 排程啟動：週一至五 15:05 盤後")
    except Exception as e:
        print(f"[MLS] 排程未啟動（不影響 API）：{e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap()
    _setup_scheduler(app)
    yield
    sched = getattr(app.state, "sched", None)
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="MLS v4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


# ══════════ 前端 ══════════
@app.get("/", response_class=HTMLResponse)
def index():
    p = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>MLS v4.0</h1><p>前端檔案未找到</p>")


@app.get("/app.html", response_class=HTMLResponse)
def full_app():
    """完整 MLS 功能頁（保留原本的個股決策、雷達、統計與李佛摩分頁）。"""
    p = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>MLS v4.0</h1><p>完整功能頁未找到</p>", status_code=404)


@app.get("/history_page_demo.html", response_class=HTMLResponse)
def history_page_demo():
    """提供使用者指定的盤中歷史／盤後蓋章回溯頁。"""
    p = os.path.join(WEB_DIR, "history_page_demo.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>盤後回溯頁未找到</h1>", status_code=404)


# ══════════ 真實盤後 DATA API（給 final7.html 吃）══════════
@app.get("/api/v2/today")
def api_v2_today(date: str = None):
    """return final7.html 完整 DATA 結構。date 不傳時自動 fallback 到最近一個有資料的交易日。"""
    import analyst
    import json as _json
    import time as _time
    from pathlib import Path
    from datetime import datetime
    CACHE_DIR = Path("/tmp/mls-v4-cache")
    CACHE_DIR.mkdir(exist_ok=True)
    CACHE_TTL = 1800  # 30 分鐘

    def _cached_or_build(ds: str, force: bool = False):
        cache_file = CACHE_DIR / f"today-{ds}.json"
        if not force and cache_file.exists():
            age = _time.time() - cache_file.stat().st_mtime
            if age < CACHE_TTL:
                try:
                    payload = _json.loads(cache_file.read_text())
                    payload["cache_hit"] = True
                    payload["cache_age_sec"] = int(age)
                    return payload
                except Exception:
                    pass  # cache 壞了就重建
        data = analyst.build_data(ds)
        payload = {"date": ds, "data": data, "ts": int(_time.time())}
        try:
            cache_file.write_text(_json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass
        return payload

    if date is None:
        # 自動找最近一個有資料的交易日（先查 cache,fallback 過去日也吃 cache）
        d = datetime.now()
        for _ in range(10):
            ds = d.strftime("%Y%m%d")
            payload = _cached_or_build(ds)
            if payload.get("data"):
                payload["auto_fallback"] = (ds != datetime.now().strftime("%Y%m%d"))
                return payload
            d = d.replace(day=d.day-1) if d.day > 1 else d.replace(month=d.month-1, day=28)
        return {"date": "", "data": [], "ts": int(_time.time())}
    return _cached_or_build(date)


@app.get("/api/v2/radar")
def api_v2_radar():
    """抗跌股雷達：落難族群 + 法人未斷 + 逆勢"""
    import analyst
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    data = analyst.build_data(today)
    picks = [d for d in data if d["quad"] in ("in_down", "out_down")
             and d["relative"]["vs_sector"] > 0
             and (d["chip_detail"]["inst_net_20d_lots"] > 0 or d["chip_detail"]["inst_streak"] >= 3)]
    return {"picks": picks, "total": len(data)}


@app.get("/api/v2/funnel")
def api_v2_funnel(code: str = None):
    """漏斗 3 關（拿掉大戶關）"""
    import analyst
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    data = analyst.build_data(today)
    if code:
        d = next((x for x in data if x["code"] == code), None)
        return d["risk"] if d else {}
    return data


@app.get("/api/v2/livermore")
def api_v2_livermore(code: str = None):
    """李佛摩六欄"""
    import analyst
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    data = analyst.build_data(today)
    if code:
        d = next((x for x in data if x["code"] == code), None)
        return d["liv_history"] if d else []
    return {"stocks": [{"code": d["code"], "name": d["name"], "state": d["livermore"]["state"]} for d in data]}


@app.get("/api/v2/stats")
def api_v2_stats():
    """統計驗證（簡化版 — 等真正 N 日資料累積後做完整版）"""
    import analyst
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    data = analyst.build_data(today)
    quads = {"in_up": 0, "in_down": 0, "out_up": 0, "out_down": 0}
    for d in data:
        quads[d["quad"]] += 1
    return {
        "universe": len(data),
        "quad_distribution": quads,
        "ready": sum(1 for d in data if d["grade"] == "Ready"),
        "watch": sum(1 for d in data if d["grade"] == "Watch"),
        "hold": sum(1 for d in data if d["grade"] == "Hold"),
    }


# ══════════ API ══════════
def _eod_row_to_api(r):
    """dec_health 一列 → 前端需要的完整結構。"""
    import funnel
    ev = dict(r)
    ev["above_ma20"] = bool(r["above_ma20"])
    # 重建承接明細與風險（從已存欄位）
    return ev


@app.get("/api/health")
def api_health():
    """系統健康檢查。"""
    return {"status": "ok", "data_mode": C.DATA_MODE,
            "broker_connected": broker.is_connected(),
            "date": db.today()}


@app.get("/api/eod")
def api_eod(date: str = None):
    """盤後個股決策資料（含五模組、承接、相對強弱、風險、交易計畫）。"""
    rows = db.load_dec_health(date)
    # 空資料自動補跑（雙保險：確保 API 永不回空）
    if not rows and date is None:
        try:
            after_hours.run_eod(db.today())
            report.daily_report(db.today())
            rows = db.load_dec_health(None)
        except Exception as e:
            return JSONResponse(
                {"date": db.today(), "stocks": [],
                 "error": f"自動補跑盤後失敗：{e}"}, status_code=200)
    out = []
    for r in rows:
        d = dict(r)
        d["above_ma20"] = bool(r["above_ma20"])
        # 補承接明細
        import chips as _chips
        import absorption
        snap = {"code": r["code"], "change_rate": r["chg"],
                "close": r["close"], "prev_close": r["prev_close"],
                "aflow": 0}
        chip = {"foreign_lots": r["foreign_lots"],
                "margin_trend": "下降" if r["stars"] >= 4 else "增加",
                "big_holder_trend": r["big_holder_trend"]}
        _, abs_detail = absorption.evaluate(snap, chip)
        d["absorption"] = abs_detail
        # 風險
        d["risk"] = decision.risk_check(
            snap, r["ratio_src"], bool(r["above_ma20"]), r["ma20"],
            r["close"], r["trigger"], r["chip_ok"])
        d["trade_plan"] = decision.trade_plan(r["track"], r["close"], r["ma20"], r["trigger"])
        d["quad_history"] = [x["quadrant"] for x in db.quad_history(r["code"], 5)]
        out.append(d)
    return {"date": date or db.today(), "stocks": out}


@app.get("/api/radar")
def api_radar(date: str = None):
    """抗跌股雷達：落難族群逆勢股。
    優先 DB 真實 (inst_daily + price_daily) → fallback dec_health (demo)。"""
    import config as C
    if date is None:
        # 找 DB 裡最近一個有 inst_daily 資料的交易日
        with db._conn() as c:
            r = c.execute("SELECT MAX(trade_date) d FROM inst_daily").fetchone()
        date = r["d"] if r and r["d"] else db.today()
    evals = []
    # 從 DB 真實資料組 evals
    prices = {p["code"]: p for p in db.load_price_daily(date)}
    insts = {i["code"]: i for i in db.load_inst_daily(date)}
    if prices and insts:
        # 算 20 日法人合計 + streak（從 inst_daily 跨日累加）
        for code, inst in insts.items():
            recent = db.load_inst_recent(code, days=20)
            recent = list(reversed(recent))  # 舊→新
            net_20 = sum((r["foreign_lots"] or 0) + (r["invest_lots"] or 0)
                         + (r["dealer_lots"] or 0) for r in recent)
            # streak：最近一日同方向連續天數
            streak = 0
            for r in reversed(recent):
                d = (r["foreign_lots"] or 0) + (r["invest_lots"] or 0) + (r["dealer_lots"] or 0)
                if d == 0:
                    break
                s = 1 if d > 0 else -1
                if streak == 0:
                    streak = s
                elif (streak > 0) == (s > 0):
                    streak += s
                else:
                    break
            p = prices.get(code)
            if not p:
                continue
            # 大盤 chg（DB 沒有，從 price_daily 算加權平均太複雜，先抓 market chg 0）
            evals.append({
                "code": code, "name": inst["name"],
                "quad": "out_down",  # 用雷達觸發：全部視為「落難」候選，由 5 日法人方向篩選
                "chg": p["change_pct"] or 0,
                "close": p["close"], "prev_close": p["prev_close"],
                "high": p["high"], "low": p["low"],
                "vs_sector": 0,  # 雷達只看法人 vs_sector 暫不卡
                "inst_net_20d": net_20,
                "inst_streak": streak,
                "big_holder_trend": None,
                "foreign_lots": inst["foreign_lots"],
                "invest_lots": inst["invest_lots"],
                "sector_chg": 0,
            })
        # vs_sector：個股 chg - 族群平均 chg
        from collections import defaultdict
        sec_sum = defaultdict(float); sec_cnt = defaultdict(int)
        for ev in evals:
            # 取 sector（從 config.UNIVERSE）
            sec = C.UNIVERSE.get(ev["code"], (None, None, None))[1]
            if sec and ev["chg"] is not None:
                sec_sum[sec] += ev["chg"]; sec_cnt[sec] += 1
        sec_avg = {s: sec_sum[s] / sec_cnt[s] for s in sec_sum if sec_cnt[s] > 0}
        for ev in evals:
            sec = C.UNIVERSE.get(ev["code"], (None, None, None))[1]
            if sec:
                ev["sector_chg"] = round(sec_avg.get(sec, 0), 2)
                ev["vs_sector"] = round(ev["chg"] - ev["sector_chg"], 2)
    else:
        # fallback dec_health (demo)
        rows = db.load_dec_health(date)
        evals = [dict(r) for r in rows]
    picks = after_hours.resilient_radar(evals)
    return {"date": date, "src": "db" if prices and insts else "demo", "picks": picks}


@app.get("/api/watchlist")
def api_watchlist(target_date: str = None):
    """觀察清單（明日標的）。"""
    td = target_date or after_hours._next_trade_date(db.today())
    return {"target_date": td, "rows": db.load_watchlist(td)}


@app.get("/api/funnel")
def api_funnel(date: str = None):
    """漏斗四關明細。"""
    import funnel
    rows = db.load_dec_health(date)
    evals = [dict(r) for r in rows]
    return funnel.run_funnel(evals)


@app.get("/api/stats")
def api_stats():
    """統計驗證：分軌勝率、四象限、分數區間。"""
    return after_hours.stats()


@app.get("/api/history")
def api_history(date: str = None, code: str = None, days: int = 5):
    """盤後蓋章歷史分類與個股跨日軌跡。"""
    dates = db.history_dates(30)
    selected = date or (dates[0] if dates else db.today())
    rows = db.load_dec_health(selected)
    output = []
    for row in rows:
        group = history.classify_eod(row)
        item = {"code": row["code"], "name": row["name"], "sector": row["sector"],
                "close": row["close"], "chg": row["chg"], "score": row["score"],
                "quad": row["quad"], "ratio": row["ratio"], "stars": row["stars"],
                "grade": row["grade"], "above_ma20": bool(row["above_ma20"]), **group}
        output.append(item)
    if code:
        trail = []
        for row in db.stock_history(code, days, through=selected):
            group = history.classify_eod(row)
            trail.append({"date": row["trade_date"], "score": row["score"],
                          "grade": row["grade"], **group})
        return {"date": selected, "dates": dates, "code": code,
                "trail": trail, "trend": history.classify_trend(trail)}
    groups = {name: [r for r in output if r["group"] == name]
              for name in ("可操作", "觀察", "排除")}
    return {"date": selected, "dates": dates, "groups": groups,
            "total": len(output)}


@app.get("/api/livermore")
def api_livermore(code: str = None, date: str = None, days: int = 20):
    """李佛摩六欄。無 code → 回全池狀態清單；有 code → 該檔逐日紀錄。"""
    dates = db.history_dates(30)
    selected = date or (dates[0] if dates else db.today())
    if code:
        return {"code": code, "cols": livermore.COLS,
                "colors": livermore.COLORS,
                "records": [{"date": r["trade_date"][5:], "state": r["state"],
                             "price": r["price"], "pivot": bool(r["pivot"])}
                            for r in db.liv_history_through(code, selected, days)]}
    rows = db.liv_snapshot(selected)
    stocks = [{"code": r["code"], "name": r["name"], "state": r["state"]} for r in rows]
    return {"date": selected, "dates": dates, "cols": livermore.COLS,
            "colors": livermore.COLORS, "stocks": stocks}


@app.get("/api/events")
def api_events(date: str = None):
    """盤中變化事件流。"""
    return {"events": db.load_events(date)}


@app.get("/api/reports")
def api_reports():
    """報告庫清單。"""
    return {"reports": report.list_reports()}


@app.post("/api/run-eod")
def api_run_eod(date: str = None):
    """手動觸發盤後（測試/補跑用）。"""
    s = after_hours.run_eod(date)
    report.daily_report(date)
    return s


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("MLS_PORT", "8000")))
