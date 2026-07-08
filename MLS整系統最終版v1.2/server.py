"""
MLS 標準版 — server.py(完整版)
排程 + 服務層,把所有模組接成閉環:

  08:30  載入今日觀察清單(SQLite watchlist)
  08:55  開盤重驗(after_hours.reverify_watchlist)
  09:00–13:35  盤中主迴圈:
        engine.build_state → 新訊號 diff → SQLite 落地
        → 現金閘門 → Telegram 分級推播(冷卻)
        → 族群新鎖定推播 → 每5分鐘族群快照落地
  15:05  盤後複查:收盤驗證命中率 → 明日觀察清單
        → Airtable 同步 → Telegram 摘要
  其他時段:每5分鐘輕量更新一次畫面(非交易時段提示)

啟動:
  pip install shioaji fastapi uvicorn pandas python-dotenv
  環境變數(.env 亦可):
    SHIOAJI_API_KEY=        ← 必填(留空位,使用者自行填入)
    SHIOAJI_SECRET_KEY=     ← 必填
    FINMIND_TOKEN=          ← 選填(籌碼,空則走匿名額度)
    TELEGRAM_BOT_TOKEN=     ← 選填(空則推播走 console dry-run)
    TELEGRAM_CHAT_ID=       ← 選填
    AIRTABLE_TOKEN=         ← 選填(空則學習資料僅存本地 SQLite)
    AIRTABLE_BASE_ID=       ← 選填
  python server.py  →  http://127.0.0.1:8000
"""

import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

load_dotenv()

import config as C
import engine
import db
import notifier
import after_hours

TW_TZ = timezone(timedelta(hours=8))

STATE = {"status": "starting"}
LOCK = threading.Lock()

_watchlist_codes = set()
_pushed_lock_sectors = set()       # 今日已推播過鎖定的族群
_last_sector_snapshot = 0.0
_did_reverify = ""                 # 已執行開盤重驗的日期
_did_afterhours = ""               # 已執行盤後複查的日期
_last_full_state = None            # 收盤前最後一輪(供盤後複查)
_sig_watch = {}                    # code → {"stop":x, "failed":bool} 今日訊號追蹤
_consec_fails = 0                  # 連續停損計數(回撤斷路器)
_breaker_on = False                # True=當日停發新進場訊號


def _now():
    return datetime.now(TW_TZ)


def _hm():
    return _now().strftime("%H:%M")


def _is_trade_day():
    return _now().weekday() < 5


def load_today_watchlist():
    global _watchlist_codes
    wl = db.load_watchlist(db.today())
    _watchlist_codes = {w["stock_id"] for w in wl if not w.get("demoted")}
    if wl:
        print(f"[server] 今日觀察清單 {len(wl)} 檔(有效 {len(_watchlist_codes)})")


def check_stops(state):
    """回撤斷路器:訊號後跌破建議停損=失敗;連3敗當日停發進場。"""
    global _consec_fails, _breaker_on
    prices = {x["code"]: x["price"] for x in state.get("stocks", [])}
    for code, w in _sig_watch.items():
        if w["failed"] or not w.get("stop"):
            continue
        p = prices.get(code)
        if p is not None and p < w["stop"]:
            w["failed"] = True
            _consec_fails += 1
            if _consec_fails >= 3 and not _breaker_on:
                _breaker_on = True
                notifier.push_summary(
                    "⛔ *回撤斷路器啟動*:當日連續 3 筆訊號觸及停損,"
                    "今日停止發送新進場訊號(記錄與學習照常,出場訊號不受影響)")


def handle_new_signals(state):
    """
    diff 出「本輪新出現」的可推播事件:
      · buy(entry/entry_high)與 watch(potential):同股當日未曾推過才推
      · sell(risk):交由 notifier 冷卻控制(持股風險要重複提醒)
    全部訊號無論推播與否都寫入 SQLite(學習用)。
    觀察清單命中即時標記。
    """
    # 龍頭股若在觀察清單 → 標記命中(龍頭不在 stocks 表內,需另行處理)
    for l in state.get("leaders", []):
        if l["code"] in _watchlist_codes:
            db.mark_watch_hit(db.today(), l["code"])

    for s in state.get("stocks", []):
        # 2026-07-08 改:watch 拆成 watch_buy(進場觀察)/ watch_exit(退場觀察)
        if s["action"] not in ("buy", "watch_buy", "watch_exit", "watch", "sell"):
            continue
        first_today = not db.signaled_today(s["code"])
        # 進場觀察(buy/watch_buy)首次推;賣出與退場觀察由冷卻控制(重複提醒)
        should_push = (
            (s["action"] in ("buy", "watch_buy", "watch") and first_today)
            or s["action"] in ("sell", "watch_exit")
        )
        # 斷路器:進場訊號停發(記錄照常);成功一筆則重置連敗
        if s["action"] == "buy" and _breaker_on:
            should_push = False
        pushed = False
        if should_push:
            pushed = notifier.push_signal(s)      # 內含冷卻
        if s["action"] == "buy" and s["code"] not in _sig_watch:
            _sig_watch[s["code"]] = {"stop": s.get("suggested_stop"),
                                     "failed": False}
        db.insert_signal(s, pushed=pushed)
        if s.get("is_watchlist_hit"):
            db.mark_watch_hit(db.today(), s["code"])


def handle_sector_locks(state):
    global _pushed_lock_sectors
    for sec in state.get("sectors", []):
        if sec["locked"] and sec["name"] not in _pushed_lock_sectors:
            if notifier.push_sector_lock(sec):
                _pushed_lock_sectors.add(sec["name"])


def scheduler_loop():
    global STATE, _did_reverify, _did_afterhours, \
           _last_sector_snapshot, _last_full_state, _pushed_lock_sectors

    load_today_watchlist()

    while True:
        try:
            hm, today = _hm(), db.today()

            # ── 跨日重置 ─────────────────────────────
            if _did_reverify and _did_reverify != today:
                _pushed_lock_sectors = set()

            if not _is_trade_day():
                time.sleep(300)
                continue

            # ── 08:30 載清單 / 08:55 開盤重驗 ─────────
            if "08:30" <= hm < "09:00":
                if _did_reverify != today and hm >= "08:55":
                    import scoring
                    scoring.reset_aflow()        # 每日開盤重置主動淨流
                    global _sig_watch, _consec_fails, _breaker_on
                    _sig_watch, _consec_fails, _breaker_on = {}, 0, False
                    engine.reload_entry_min()    # 載入盤後調整過的門檻
                    load_today_watchlist()
                    after_hours.reverify_watchlist()
                    load_today_watchlist()        # 重驗後重載(剔除降級)
                    _did_reverify = today
                time.sleep(30)
                continue

            # ── 09:00–13:35 盤中主迴圈 ────────────────
            if "09:00" <= hm <= "13:35":
                state = engine.build_state(watchlist_codes=_watchlist_codes)
                _last_full_state = state
                check_stops(state)
                handle_new_signals(state)
                handle_sector_locks(state)
                if time.time() - _last_sector_snapshot >= 300:   # 每5分鐘
                    db.insert_sector_snapshot(state["_sectors_full"])
                    _last_sector_snapshot = time.time()
                with LOCK:
                    STATE = {k: v for k, v in state.items()
                             if not k.startswith("_")}
                print(f"[loop] {hm} 鎖定={state['locked_sectors']} "
                      f"龍頭={[l['code'] for l in state['leaders']]} "
                      f"訊號={len(state['stocks'])}")
                time.sleep(C.SCAN_INTERVAL_SEC)
                continue

            # ── 15:05 盤後複查(一天一次;state 為空時兜底重抓) ──
            if hm >= "15:05" and _did_afterhours != today:
                state_for_eod = _last_full_state
                if state_for_eod is None:
                    print("[server] 盤中 state 缺失,EOD 兜底重抓收盤快照…")
                    try:
                        import eod_pipeline
                        snaps = eod_pipeline.fetch_eod_snaps()
                        import engine as _e
                        secs = _e.compute_sector_flow(snaps)
                        state_for_eod = {"_snaps": snaps,
                                         "_sectors_full": [
                                             {k: v for k, v in s.items() if k != "members"}
                                             for s in secs],
                                         "stocks": [], "sectors": []}
                    except Exception as e:
                        print(f"[server] 兜底重抓失敗:{e}")
                if state_for_eod is not None:
                    print("[server] 執行盤後複查…")
                    after_hours.run(state_for_eod)
                    _did_afterhours = today
                time.sleep(60)
                continue

            # ── 非交易時段:輕量更新畫面 ───────────────
            # 修盤後覆蓋 bug:盤中有資料時保留 _last_full_state,不讓空 STATE 蓋掉
            if _last_full_state is not None:
                # 用盤中最後一輪凍結的資料當 STATE,只在 is_market_hours 標記更新
                with LOCK:
                    STATE = {k: v for k, v in _last_full_state.items()
                             if not k.startswith("_")}
                    STATE["is_market_hours"] = False
                    STATE["updated_at"] = datetime.now(TW_TZ).isoformat()
            else:
                # 冷啟動:還沒跑過盤中才 build_state
                state = engine.build_state(watchlist_codes=_watchlist_codes)
                _last_full_state = state
                with LOCK:
                    STATE = {k: v for k, v in state.items()
                             if not k.startswith("_")}
            time.sleep(300)

        except Exception as e:
            traceback.print_exc()
            with LOCK:
                STATE = {**STATE, "error": str(e)}
            time.sleep(30)


# ══════════════════════════════════════════════════════
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    """2026-07-08: 因 uvicorn server:app module mode 不走 __main__,
    scheduler_loop 不會自動啟動。在 lifespan 啟動時補建 scheduler + 完成 db.init()。"""
    db.init()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    yield

app = FastAPI(title="MLS Standard", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/api/state")
def api_state():
    with LOCK:
        return JSONResponse(STATE)


@app.get("/api/review")
def api_review():
    """近30日命中率 + 今日統計(前端學習區/複盤頁用)"""
    return JSONResponse({
        "recent_hit_rates": db.recent_hit_rates(30),
        "today": db.today_stats(),
        "watchlist_today": db.load_watchlist(db.today()),
    })


@app.get("/")
def home():
    # 2026-07-08:首頁 = index.html,已內含板塊族群卡片 + 群組分類熱力表卡片設計
    html = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/sectors")
def sectors_page():
    """獨立 sectors.html 保留(測試版,可移除)"""
    html = Path(__file__).with_name("sectors.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# ══════════════════════════════════════════════════════
# 插件掛鉤端點(v1.3 NEXORA / v1.4 EOD / v1.5 排行)
# 純插件:讀主系統既有資料,失敗降級為提示,不影響主流程
# ══════════════════════════════════════════════════════

@app.get("/api/eod_rank")
def api_eod_rank():
    """排行插件:盤後榜單(資料源 = EOD 管線 training_samples/sector_daily)。"""
    try:
        import rankings_api
        return JSONResponse(rankings_api.eod_rankings())
    except Exception as e:
        return JSONResponse({"date": None, "note": f"插件錯誤:{e}"})


@app.get("/rankings")
def rankings_page():
    """排行插件頁(盤中/盤後 五榜 + 族群卡)。"""
    try:
        html = Path(__file__).with_name("rankings.html").read_text(encoding="utf-8")
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"rankings.html 缺失:{e}", status_code=500)


@app.get("/api/nexora")
def api_nexora():
    """NEXORA 插件當日報告(無報告時回提示)。"""
    try:
        from pathlib import Path as _P
        import glob
        files = sorted(glob.glob(str(_P(__file__).parent / "reports" / "NEXORA_*.md")))
        if not files:
            return JSONResponse({"report": None, "note": "尚無報告,盤後 15:05 產出"})
        return JSONResponse({"report": _P(files[-1]).read_text(encoding="utf-8"),
                             "file": files[-1]})
    except Exception as e:
        return JSONResponse({"report": None, "error": str(e)})


@app.get("/sectors")
def sectors_page():
    """板塊→個股卡片式 UI(2026-07-08 新增,外部頁面,不動主邏輯)。"""
    try:
        html = Path(__file__).with_name("sectors.html").read_text(encoding="utf-8")
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"sectors.html 缺失:{e}", status_code=500)


@app.get("/api/nexora-v2")
def api_nexora_v2():
    """NEXORA V2.0 獨立頁面用 API(2026-07-08 新增):
    直接呼叫 nexora.run_report() 跑完整 12 節報告,
    不動主邏輯,僅新增外部 endpoint + 頁面。"""
    try:
        watchlist_codes = _watchlist_codes
        full_state = engine.build_state(watchlist_codes=watchlist_codes)
        import nexora
        # 傳空 rotation_reports(避免在 API 呼叫時觸發 after_hours 推播)
        report = nexora.run_report(full_state, [])
        return JSONResponse({"date": full_state.get("market", {}).get("time", ""),
                             "report": report})
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"report": None, "error": str(e)}, status_code=500)


@app.get("/nexora")
def nexora_page():
    """NEXORA V2.0 獨立頁面(2026-07-08 新增)。"""
    try:
        html = Path(__file__).with_name("nexora-v2.html").read_text(encoding="utf-8")
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"nexora-v2.html 缺失:{e}", status_code=500)
        return JSONResponse({"report": None, "error": str(e)})


if __name__ == "__main__":
    db.init()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000)
