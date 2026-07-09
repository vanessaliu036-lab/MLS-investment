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
from contextlib import asynccontextmanager
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.encoders import jsonable_encoder
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

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
        if s["action"] not in ("buy", "watch", "sell"):
            continue
        first_today = not db.signaled_today(s["code"])
        should_push = (
            (s["action"] in ("buy", "watch") and first_today)
            or s["action"] == "sell"
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
                    scoring.reset_bs()           # 每日開盤重置BS濾網近端估算
                    scoring.reset_all_trackers()  # 每日開盤重置 TickTracker(第四條件)
                    global _sig_watch, _consec_fails, _breaker_on
                    _sig_watch, _consec_fails, _breaker_on = {}, 0, False
                    engine.reload_entry_min()    # 載入盤後調整過的門檻
                    load_today_watchlist()
                    after_hours.reverify_watchlist()
                    load_today_watchlist()        # 重驗後重載(剔除降級)
                    _did_reverify = today
                time.sleep(30)
                continue

            # ── 09:00–13:35 盤中主迴圈(收盤後也跑,讓資料庫保持熱)────
            if True:
                try:
                    state = engine.build_state(watchlist_codes=_watchlist_codes)
                    _last_full_state = state
                    if "09:00" <= hm <= "13:35":
                        check_stops(state)
                        handle_new_signals(state)
                        handle_sector_locks(state)
                except Exception as e:
                    print(f"[server] build_state 失敗:{e}", flush=True)
                    state = _last_full_state or {"sectors": [], "stocks": [], "locked_sectors": [], "leaders": [], "_sectors_full": [], "market": {"index": 0, "index_pct": 0, "amount_100m": 0, "score": 0, "mode": "—", "time": hm}, "is_market_hours": False}
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
            state = engine.build_state(watchlist_codes=_watchlist_codes)
            _last_full_state = _last_full_state or state
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
app = FastAPI(title="MLS Standard")


@asynccontextmanager
async def lifespan(app):
    """uvicorn 啟動時啟動 scheduler_loop,停機時自動結束。"""
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    print("[server] scheduler_loop started via lifespan", flush=True)
    yield
    print("[server] shutting down scheduler_loop", flush=True)


app.router.lifespan_context = lifespan
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/api/state")
def api_state():
    with LOCK:
        # UI 防呆:補上 health/chip/bs/quadrant/tri/doc_strategy 預設(舊 STATE 沒塞這些欄位也不會空白)
        snap = {k: v for k, v in STATE.items()}
        for s in (snap.get("stocks") or []):
            if not isinstance(s.get("health"), dict) or not s["health"].get("quadrant"):
                s["health"] = {"quadrant": "neutral", "label": "未評", "stars": "—", "desc": "資金健康度待盤後更新", "health_score": 0, "aflow_ratio": None}
            if not isinstance(s.get("chip"), dict):
                s["chip"] = {"has_data": False, "inst_net_20d_lots": None, "inst_streak": None, "big_holder_pct": None, "big_holder_trend": None}
            if not isinstance(s.get("triangulation"), dict):
                s["triangulation"] = {"verdict": "pending", "log": {}, "next_signal": "—"}
            if not isinstance(s.get("doc_strategy"), dict):
                s["doc_strategy"] = {"pass": False, "score": 0}
            if s.get("bs") is None: s["bs"] = 50
        # 補 asof 對齊 as_of (前端 rankings / strategy_compare / nexora 直接吃 as_of)
        mkt = snap.get("market") or {}
        snap["asof"] = mkt.get("time") or snap.get("updated_at") or ""
        snap["as_of"] = snap["asof"]
        return _safe(snap)


@app.get("/health")
def health():
    """Docker healthcheck 用。回 200 + 狀態摘要。"""
    with LOCK:
        n_sectors = len(STATE.get("sectors") or [])
        n_stocks = len(STATE.get("stocks") or [])
    ok = (STATE.get("status") != "starting") or (n_stocks > 0)
    return _safe({"ok": ok, "status": STATE.get("status"), "sectors": n_sectors, "stocks": n_stocks}, status_code=200 if ok else 503)


@app.get("/api/realtime_signal/{code}")
def api_realtime_signal(code: str):
    """
    文件二盤中觸發(第四條件 + 三大條件)。
    對指定股票代碼回傳 evaluate_realtime 結果。
    前端可每秒或每分鐘呼叫一次。
    """
    try:
        import scoring
        tracker = scoring.get_tracker(code)
        # 從 /api/state 拿現價 + 大盤資訊(避免重複打 Shioaji)
        with LOCK:
            st = STATE
        stock = next((x for x in st.get("stocks", []) if x.get("code") == code), None)
        mkt = st.get("market", {})
        result = scoring.evaluate_realtime(
            code=code,
            current_price=(stock or {}).get("price"),
            market_open_price=mkt.get("open"),
            market_current_price=mkt.get("index"),
            day_30m_high=(stock or {}).get("high"),
            est_volume=(stock or {}).get("total_volume"),
            prev_day_volume=(stock or {}).get("prev_day_volume"),
            tracker=tracker,
            loose_first_30min=True,
        )
        result["in_pool"] = stock is not None
        # None-safe:inf 不能 JSON
        if result.get("cum_ratio") == float('inf'): result["cum_ratio"] = None
        if result.get("recent_ratio") == float('inf'): result["recent_ratio"] = None
        return _safe(result)
    except Exception as e:
        return _safe({"error": str(e), "code": code}, status_code=500)


@app.post("/api/tick/{code}")
def api_tick(code: str, payload: dict):
    """
    餵入一筆 tick 進 TickTracker。
    期望 payload:{price: float, volume: int, ts?: float, side?: 1|-1|0}
    """
    try:
        import scoring
        tracker = scoring.get_tracker(code)
        tracker.add_tick(
            price=float(payload.get("price", 0)),
            volume=int(payload.get("volume", 0)),
            ts=payload.get("ts"),
            side=payload.get("side"),
        )
        return _safe({"ok": True,
                             "cum_ratio": (None if tracker.cum_ratio == float('inf') else tracker.cum_ratio),
                             "recent_ratio": (None if tracker.recent_5min_ratio == float('inf') else tracker.recent_5min_ratio)})
    except Exception as e:
        return _safe({"error": str(e)}, status_code=500)


@app.get("/api/review")
def api_review():
    """近30日命中率 + 今日統計(前端學習區/複盤頁用)"""
    return _safe({
        "recent_hit_rates": db.recent_hit_rates(30),
        "today": db.today_stats(),
        "watchlist_today": db.load_watchlist(db.today()),
    })


@app.get("/api/watchlist/{trade_date}")
def api_watchlist(trade_date: str):
    """盤後產出的「{trade_date} 觀察清單」(after_hours.build_tomorrow_watchlist)。"""
    try:
        rows = db.load_watchlist(trade_date)
        # 補上開盤重驗狀態
        return _safe({
            "trade_date": trade_date,
            "n": len(rows),
            "n_active": sum(1 for r in rows if not r.get("demoted")),
            "n_demoted": sum(1 for r in rows if r.get("demoted")),
            "n_hit": sum(1 for r in rows if r.get("hit")),
            "rows": rows,
        })
    except Exception as e:
        traceback.print_exc()
        return _safe({"error": str(e)}, status_code=500)


@app.get("/api/review/{trade_date}")
def api_review_date(trade_date: str):
    """單日盤後複查:命中率 / 漏抓股 / 觀察清單"""
    try:
        with db._lock, db._conn() as c:
            review = None
            try:
                row = c.execute("SELECT * FROM review_log WHERE trade_date=?", (trade_date,)).fetchone()
                if row:
                    review = dict(row)
                    try:
                        review["missed"] = json.loads(review.get("missed") or "[]")
                    except Exception:
                        review["missed"] = []
            except Exception:
                pass
            # signals 表無 ai_score 欄位(見 db.py:27),改用 triggered_rules 數量+confidence_label 排序
            signals_today = [dict(r) for r in c.execute(
                "SELECT * FROM signals WHERE trade_date=? ORDER BY confidence_label DESC, ts DESC",
                (trade_date,))]
        watchlist = db.load_watchlist(trade_date)
        # 拆 triggered_rules 統計每筆觸發的規則數(給前端當熱度)
        for s in signals_today:
            try:
                s["_rules"] = json.loads(s.get("triggered_rules") or "[]")
            except Exception:
                s["_rules"] = []
        return _safe({
            "trade_date": trade_date,
            "review": review,
            "watchlist": watchlist,
            "signals_count": len(signals_today),
            "top_signals": signals_today[:10],
        })
    except Exception as e:
        traceback.print_exc()
        return _safe({"error": str(e)}, status_code=500)


@app.get("/api/eod_rank")
def api_eod_rank():
    """排行插件:盤後榜單(資料源 = EOD 管線 training_samples/sector_daily)。"""
    try:
        import rankings_api
        return _safe(rankings_api.eod_rankings())
    except Exception as e:
        return _safe({"date": None, "note": f"插件錯誤:{e}"})


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
            return _safe({"report": None, "note": "尚無報告,盤後 15:05 產出"})
        return _safe({"report": _P(files[-1]).read_text(encoding="utf-8"),
                             "file": files[-1]})
    except Exception as e:
        return _safe({"report": None, "error": str(e)})


@app.get("/")
def home():
    html = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/signals")
def signals_page():
    """v1.8 進出場訊號板頁面。"""
    try:
        html = Path(__file__).with_name("signals.html").read_text(encoding="utf-8")
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"signals.html 缺失:{e}", status_code=500)


# ══════════════════════════════════════════════════════
# 李佛摩 v10 篩選插件(2026-07-09)
# 六點轉向邏輯:趨勢 / 自然回撤 / 突破 / 跌破 / 量能 / 警示
# 純讀 STATE + broker.daily_kbars,不動主邏輯
# ══════════════════════════════════════════════════════
@app.get("/api/livermore")
def api_livermore():
    try:
        import livermore
        full_state = engine.build_state(watchlist_codes=_watchlist_codes)
        stocks = full_state.get("stocks") or []

        def _kbars(code):
            import broker as _br
            return _br.daily_kbars(code, days=70)

        rows = livermore.screen_pool(stocks, _kbars)
        return _safe({
            "as_of": (full_state.get("market") or {}).get("time", ""),
            "n": len(rows),
            "buy_n": sum(1 for r in rows if r.get("signal", "").startswith("buy")),
            "sell_n": sum(1 for r in rows if "sell" in r.get("signal", "") or "breakdown" in r.get("signal", "")),
            "watch_n": sum(1 for r in rows if r.get("signal", "").startswith("watch")),
            "rows": rows,
        })
    except Exception as e:
        traceback.print_exc()
        return _safe({"error": str(e)}, status_code=500)


@app.get("/api/livermore/{code}")
def api_livermore_stock(code: str):
    try:
        import livermore
        import broker as _br
        full_state = engine.build_state(watchlist_codes=_watchlist_codes)
        snap = next((s for s in (full_state.get("stocks") or []) if s.get("code") == code), None)
        if snap is None:
            return _safe({"error": f"個股 {code} 不在觀察池"}, status_code=404)
        kbars = _br.daily_kbars(code, days=70)
        return _safe(livermore.analyze_stock(snap, kbars))
    except Exception as e:
        traceback.print_exc()
        return _safe({"error": str(e)}, status_code=500)


@app.get("/preview_v23")
def preview_v23():
    """v2.3 UI preview with mocked state (只用於截圖驗收,看 UI 是否你喜歡)。"""
    try:
        html = Path(__file__).with_name("preview_v23.html").read_text(encoding="utf-8")
        return HTMLResponse(html)
    except FileNotFoundError:
        return HTMLResponse(
            "<h3 style='font-family:sans-serif;padding:24px'>preview_v23.html 尚未生成</h3>"
            "<p style='font-family:sans-serif;padding:0 24px'>這條路由只供設計稿截圖用,檔案不存在就 200 回空殼,不影響上線。</p>",
            status_code=200,
        )


@app.get("/v23_mock_design")
def v23_mock_design():
    """v2.3 靜態設計稿(個股卡片第二層 + 頂部 banner 純展示)。"""
    try:
        html = Path(__file__).with_name("v23_mock_design.html").read_text(encoding="utf-8")
        return HTMLResponse(html)
    except FileNotFoundError:
        return HTMLResponse(
            "<h3 style='font-family:sans-serif;padding:24px'>v23_mock_design.html 尚未生成</h3>",
            status_code=200,
        )


# ══════════════════════════════════════════════════════
# 四大策略對比插件(2026-07-09)
# 照你文件的四大條件獨立評分:MA20/乖離率/量>5日均/收>昨高
# 與主系統 AI 分數 A/B 對比,所有資料來自主系統 STATE
# 純插件不動主邏輯
# ══════════════════════════════════════════════════════
@app.get("/api/strategy_compare")
def api_strategy_compare():
    try:
        watchlist_codes = _watchlist_codes
        full_state = engine.build_state(watchlist_codes=watchlist_codes)
        rows = []
        for s in (full_state.get("stocks") or []):
            factors = s.get("factors") or {}
            ai = s.get("ai_score") or 0
            # 文件四大條件(用既有 factors 加 change_rate 推算,純讀)
            cond_ma20  = (factors.get("trend") or 0) >= 18          # 趨勢 >= 18/25 視為站上 MA20
            cond_bias  = abs(s.get("change_rate") or 0) < 8          # 乖離率 < 8%
            cond_vol5  = (s.get("volume_ratio") or 0) >= 1.3        # 量比 >= 1.3 ≈ 量 > 5日均
            cond_high  = (s.get("change_rate") or 0) > 0            # 收 > 昨高(漲)
            cond_chip  = (factors.get("chip") or 0) >= 12           # 籌碼 >= 12/20
            doc_pass = sum([cond_ma20, cond_bias, cond_vol5, cond_high, cond_chip])
            # BS 動態倍數(從 STATE 拿,沒就降級)
            bs = s.get("bs")
            bs_mult = round((bs or 50) / 50, 2) if bs is not None else None
            # AI 對比:通過 >= 4 視為強勢
            agree = "A=B" if (doc_pass >= 4 and ai >= 70) or (doc_pass <= 2 and ai < 50) else ("A>B" if ai > 80 else "B>A" if doc_pass >= 4 else "—")
            rows.append({
                "code": s.get("code"),
                "name": s.get("name"),
                "sector": s.get("sector"),
                "ai_score": ai,
                "doc_pass": doc_pass,
                "doc_score": doc_pass * 20,                    # 5 條件 × 20 分
                "cond_ma20": cond_ma20,
                "cond_bias": cond_bias,
                "cond_vol5": cond_vol5,
                "cond_high": cond_high,
                "cond_chip": cond_chip,
                "bs_mult": bs_mult,
                "agree": agree,
                "action": s.get("action"),
            })
        rows.sort(key=lambda x: (x["doc_pass"], x["ai_score"]), reverse=True)
        return _safe({"as_of": full_state.get("market", {}).get("time", ""), "n": len(rows), "rows": rows})
    except Exception as e:
        traceback.print_exc()
        return _safe({"error": str(e)}, status_code=500)


# ══════════════════════════════════════════════════════
# 資金健康度摘要插件(2026-07-09)
# 資金流向×漲跌四象限 + Level 8.1 三角交叉驗證
# 純讀主系統 STATE,只取每族群最佳代表
# ══════════════════════════════════════════════════════
@app.get("/api/money_health_summary")
def api_money_health_summary():
    try:
        watchlist_codes = _watchlist_codes
        full_state = engine.build_state(watchlist_codes=watchlist_codes)
        sectors = full_state.get("sectors") or []
        rows = []
        for sec in sectors:
            members = [s for s in (full_state.get("stocks") or []) if s.get("sector") == sec.get("name")]
            if not members:
                continue
            # 從每檔個股的 health.quadrant 統計四象限
            quad_count = {"in_up": 0, "in_down": 0, "out_up": 0, "out_down": 0}
            health_score_avg = 0
            health_n = 0
            for m in members:
                h = m.get("health") or {}
                q = h.get("quadrant")
                if q in quad_count:
                    quad_count[q] += 1
                hs = h.get("health_score")
                if hs is not None:
                    health_score_avg += hs
                    health_n += 1
            hs_avg = round(health_score_avg / health_n, 1) if health_n else None
            # 取族群代表:健康度最高那檔
            top = sorted(members, key=lambda m: (m.get("health") or {}).get("health_score", 0), reverse=True)
            rep = top[0] if top else None
            rep_h = (rep or {}).get("health") or {}
            rows.append({
                "sector": sec.get("name"),
                "sector_type": sec.get("type"),
                "pct": sec.get("pct"),
                "flow_score": sec.get("flow_score"),
                "locked": sec.get("locked"),
                "member_n": len(members),
                "quadrant": quad_count,
                "health_score_avg": hs_avg,
                "rep_code": (rep or {}).get("code"),
                "rep_name": (rep or {}).get("name"),
                "rep_quadrant": rep_h.get("quadrant"),
                "rep_label": rep_h.get("label"),
                "rep_stars": rep_h.get("stars"),
                "rep_score": rep_h.get("health_score"),
            })
        rows.sort(key=lambda x: (x.get("health_score_avg") or 0), reverse=True)
        return _safe({"as_of": full_state.get("market", {}).get("time", ""), "sectors": rows})
    except Exception as e:
        traceback.print_exc()
        return _safe({"error": str(e)}, status_code=500)


# ══════════════════════════════════════════════════════
# 資金健康度決策卡 API(2026-07-09 Phase A)
# 回傳每檔個股 6 欄位決策卡:AI Score / Confidence / State /
#   Action / Trigger / Invalidation / 進場停損目標
# 純插件:讀 STATE + 跑 annotate_with_decision
# ══════════════════════════════════════════════════════
@app.get("/api/money_health")
def api_money_health():
    """
    v3 升級(2026-07-09 Vanessa 規格):
    - 列表報告模式(不再只是點擊個股 modal)
    - 健康分 v3 公式:資金流分(0-50)+ 價量分(0-20)+ 族群分(0-5)+ Chip Score(0-25)
    - 每張卡附 Chip Score(法人買賣超→0-25 分)+ 健康分 v3 分項明細
    - 每日自動存檔到 reports/health_score_history/YYYYMMDD.json
    - 含 time_series(該股近 5 日健康分序列)+ hit_rate_stats(命中率統計)
    - 全觀察池覆蓋(不再只回觀察清單),盤後即使 tick 未連線也照跑、照存檔
    """
    try:
        # 直接讀 UNIVERSE 50 檔全觀察池,不走 engine.build_state()(它會被 total_volume
        # 過濾掉 + Shioaji session 重連很慢)。sectors 從 db 讀今日 snapshot,
        # 沒 snapshot 時用空 list(annotate 仍會跑,但 sector_pct 全 0)。
        import config as _C
        import broker as _broker
        codes = list(_C.UNIVERSE)
        snaps = _broker.batch_snapshots(codes)
        # 補 name 欄位(batch_snapshots 不給 name)
        for _s in snaps:
            if not _s.get("name"):
                _s["name"] = _C.NAME_MAP.get(_s.get("code"), _s.get("code"))
        # sectors:優先讀 STATE(已跑 scheduler_loop),fallback 用空 list
        sectors = (STATE.get("sectors") if isinstance(STATE, dict) else None) or []
        market_pct = ((STATE.get("market") or {}).get("change_rate") if isinstance(STATE, dict) else None) or 0.0
        # 跑決策卡(每檔增補 _decision)
        import money_health
        money_health.annotate_with_decision(snaps, sectors, market_pct)
        stocks = snaps  # 替換變數名,後面組 cards 用 snaps(全觀察池)
        # 組裝回傳:每檔一個決策卡
        cards = []
        for s in stocks:
            d = s.get("_decision") or {}
            h = s.get("_health") or {}
            t = s.get("_tri") or {}
            c = s.get("_chip") or {}
            cards.append({
                "code": s.get("code"),
                "name": s.get("name"),
                "sector": s.get("sector"),
                "price": s.get("price"),
                "change_rate": s.get("change_rate"),
                "quadrant": h.get("quadrant"),
                "label": h.get("label"),
                "health_score": h.get("health_score"),
                "health_v3_breakdown": h.get("health_v3_breakdown") or {},
                "aflow_ratio": h.get("aflow_ratio"),
                "verdict": t.get("verdict"),
                "strength": t.get("strength"),
                "decision": d,
                "chip": c,
                "evidence": s.get("_ev") or [],
            })
        # 排序:state (Ready > Watch > Hold) + ai_score desc
        state_order = {"Ready": 0, "Watch": 1, "Hold": 2}
        cards.sort(key=lambda c: (state_order.get(c["decision"].get("state", "Hold"), 9),
                                  -(c["decision"].get("ai_score") or 0)))

        # v3 新增:每日快照存檔 + 命中率統計
        try:
            import health_history
            snapshot_path = health_history.save_snapshot(cards, market_pct)
        except Exception as e:
            print(f"[money_health] save_snapshot failed: {e}")
            snapshot_path = None

        try:
            import health_history
            hit_rate = health_history.hit_rate_stats()
            # 取每檔 time_series(近 5 日)
            recent_snaps = health_history.load_recent_snapshots(5)
            ts_map = {}
            for c in cards:
                code = c.get("code")
                if code:
                    series = health_history.time_series_for_code(code, recent_snaps)
                    if series:
                        ts_map[code] = series
            for c in cards:
                code = c.get("code")
                if code in ts_map:
                    c["time_series"] = ts_map[code]
        except Exception as e:
            print(f"[money_health] hit_rate/time_series failed: {e}")
            hit_rate = None

        return _safe({
            "as_of": ((STATE.get("market") or {}).get("time") if isinstance(STATE, dict) else "") or "",
            "market_pct": market_pct,
            "count": len(cards),
            "ready_n": sum(1 for c in cards if c["decision"].get("state") == "Ready"),
            "watch_n": sum(1 for c in cards if c["decision"].get("state") == "Watch"),
            "hold_n": sum(1 for c in cards if c["decision"].get("state") == "Hold"),
            "cards": cards,
            "snapshot_path": snapshot_path,
            "hit_rate": hit_rate,
        })
    except Exception as e:
        traceback.print_exc()
        return _safe({"error": str(e)}, status_code=500)


# ══════════════════════════════════════════════════════
# 命中率統計獨立端點(2026-07-09)
# 給前端「模型驗證」分頁用;查 reports/health_score_history/ 所有快照
# ══════════════════════════════════════════════════════
@app.get("/api/money_health/hit_rate")
def api_money_health_hit_rate():
    """
    從 reports/health_score_history/*.json 讀所有快照,
    算健康分 ≥65 / 50-64 / <50 三組 + 四象限的隔日報酬率。
    """
    try:
        import health_history
        stats = health_history.hit_rate_stats()
        snaps = health_history.load_recent_snapshots(60)
        return _safe({
            "stats": stats,
            "snapshots": [{"date": s.get("date"), "count": s.get("count"),
                           "market_pct": s.get("market_pct")} for s in snaps],
        })
    except Exception as e:
        traceback.print_exc()
        return _safe({"error": str(e)}, status_code=500)


@app.get("/api/money_health/time_series/{code}")
def api_money_health_time_series(code: str):
    """
    個股健康分時間序列(從 reports/health_score_history 讀)。
    """
    try:
        import health_history
        snaps = health_history.load_recent_snapshots(30)
        series = health_history.time_series_for_code(code, snaps)
        return _safe({"code": code, "series": series,
                      "snapshots_n": len(snaps)})
    except Exception as e:
        traceback.print_exc()
        return _safe({"error": str(e)}, status_code=500)


# ══════════════════════════════════════════════════════
# 個股第二層 API(2026-07-09)
# 一次回全部分時/日K/法人/大戶,給 detail modal 第二層用
# 純插件不動主邏輯
# ══════════════════════════════════════════════════════
@app.get("/api/stock_detail/{code}")
def api_stock_detail(code: str):
    """
    回傳:
      snapshot    即時 OHLC + 量能(從 STATE 抓,失敗則 broker 即時取一次)
      kbars       日K 60 筆(MA20/MA60 計算 + 走勢圖)
      chips       法人/大戶(來自 chips.get_chips)— 外資/投信/自營/主力/400張/千張
      buy_sell    內外盤累計量 + BS Ratio + 主動買/賣%
      indicators  MA5/MA10/MA20/MACD/KD/RSI/ATR 技術指標
      health      個股健康度(來自 scoring + engine 既有)
      targets     買點 / 停損 / T1 / T2 / RR(規則引擎產出)
      ai_reasons  AI 結論 + 通過/未通過因子清單
    五檔報價因 Shioaji 1.5.5 公開 API 無 order_book 方法,本機無 tick stream
    無法穩定取到 → 第二層「五檔報價」tab 維持 placeholder
    """
    try:
        # 1) snapshot
        snap = None
        with LOCK:
            for s in (STATE.get("stocks") or []):
                if s.get("code") == code:
                    snap = dict(s); break
        if snap is None:
            try:
                import broker as _br
                ss = _br.batch_snapshots([code])
                snap = ss[0] if ss else {}
            except Exception:
                snap = {"code": code}
        # 清洗:把 enum 物件轉成字串(Shioaji tick_type 等)
        def _clean(v):
            if v is None: return None
            if hasattr(v, "name"): return str(v.name)
            if isinstance(v, dict): return {k: _clean(x) for k, x in v.items()}
            if isinstance(v, list): return [_clean(x) for x in v]
            return v
        snap = _clean(snap)

        # 2) kbars
        kbars = []
        try:
            import broker as _br
            kbars = _br.daily_kbars(code, days=60)
            # 加 MA20 / MA60
            closes = [k.get("close") for k in kbars if k.get("close") is not None]
            for i, k in enumerate(kbars):
                ma20 = sum(closes[max(0, i-19):i+1]) / max(1, min(20, i+1))
                ma60 = sum(closes[max(0, i-59):i+1]) / max(1, min(60, i+1))
                k["ma20"] = round(ma20, 2) if ma20 else None
                k["ma60"] = round(ma60, 2) if ma60 else None
        except Exception as e:
            print(f"[detail] kbars {code} 失敗:{e}")

        # 2.5) snapshot 缺欄位 fallback:從 kbars 最後一筆 K 補
        # broker 1.5.5 公開 API 的 snapshots 對某些股不會回 high/low/total_volume,
        # 用最後一根日 K 的 open/high/low/volume 補
        if kbars:
            last_k = kbars[-1]
            if snap.get("open") is None and last_k.get("open") is not None:
                snap["open"] = last_k["open"]
            if snap.get("high") is None and last_k.get("high") is not None:
                snap["high"] = last_k["high"]
            if snap.get("low") is None and last_k.get("low") is not None:
                snap["low"] = last_k["low"]
            if snap.get("total_volume") is None and last_k.get("volume") is not None:
                snap["total_volume"] = last_k["volume"]

        # 3) chips
        chips_data = {}
        try:
            import chips as _ch
            # 確保 _cache 已初始化(模組剛 import 時 _cache 還沒設)
            try:
                _ch._load_disk()
            except Exception:
                pass
            chips_data = _ch.get_chips(code)
            chips_data["has_data"] = chips_data.get("inst_net_20d_lots") is not None
        except Exception as e:
            print(f"[detail] chips {code} 失敗:{e}")
            chips_data = {"has_data": False,
                          "inst_net_20d_lots": None, "inst_streak": None,
                          "big_holder_pct": None, "big_holder_trend": None,
                          "foreign_net_20d": None, "trust_net_20d": None,
                          "dealer_net_20d": None, "main_force_net_20d": None,
                          "foreign_5d": None, "foreign_10d": None,
                          "inst_5d": None, "inst_10d": None,
                          "holder_400_pct": None, "holder_400_trend": None}

        # 4) buy_sell BS Ratio + 主動買賣%
        bv = snap.get("buy_volume") or 0
        sv = snap.get("sell_volume") or 0
        bs_pct = None
        if bv + sv > 0:
            bs_pct = round(bv / (bv + sv) * 100, 1)
        active_buy_pct = bs_pct              # 主動買% == BS% (外盤/(外+內))
        active_sell_pct = round(100 - bs_pct, 1) if bs_pct is not None else None

        # 5) 技術指標
        indicators = {}
        try:
            import indicators as _ind
            indicators = _ind.compute_all(kbars)
        except Exception as e:
            print(f"[detail] indicators {code} 失敗:{e}")
            indicators = {"ok": False, "reason": str(e)}

        # 6) 個股健康度 — 直接從 STATE 找該股,既有 health_score/quadrant/label/aflow_ratio
        health = {"health_score": None, "quadrant": None, "label": None,
                  "stars": None, "aflow_ratio": None}
        try:
            for _s in (STATE.get("stocks") or []):
                if _s.get("code") == code:
                    health = {
                        "health_score": _s.get("health_score"),
                        "quadrant": _s.get("quadrant"),
                        "label": _s.get("label"),
                        "stars": _s.get("stars"),
                        "aflow_ratio": _s.get("aflow_ratio"),
                    }
                    break
        except Exception:
            pass

        # 7) 交易計劃 — 買點 / 停損 / T1 / T2 / RR
        # 規則:買點 = 站上均價線且昨日收 + 微量緩衝;停損 = 既有 suggested_stop;
        #       T1 = 買點 + 1R;T2 = 買點 + 2R;RR = (T1-買點)/(買點-停損)
        targets = {"advice": "等待", "buy": None, "stop": None,
                   "t1": None, "t2": None, "rr": None}
        try:
            price = snap.get("price")
            stop = snap.get("suggested_stop") or snap.get("low")
            avgp = snap.get("avg_price")
            # 買點:均價線微上(等回測不追高)— 若已大於均價太多,退回均價 + ATR/2
            atr_v = (indicators or {}).get("atr")
            if price is not None and avgp:
                if price <= avgp * 1.005:
                    buy = round(price, 2)              # 已回均價附近,直接可進
                elif atr_v:
                    buy = round(avgp + atr_v * 0.5, 2)
                else:
                    buy = round(avgp * 1.003, 2)
            elif price is not None:
                # 沒均價就用昨日收 + 0.5%
                closes = [k.get("close") for k in (kbars or []) if k.get("close") is not None]
                if len(closes) >= 2:
                    buy = round(closes[-2] * 1.005, 2)
            # T1 / T2 / RR
            if buy and stop and buy > stop:
                r1 = buy - stop
                t1 = round(buy + r1, 2)
                t2 = round(buy + r1 * 2, 2)
                rr = round(r1 / r1, 2) if r1 > 0 else None      # = 1.0 嚴格定義
                # 但 Vanessa 訊息 RR=2.8 是 (T1-buy)/(buy-stop) 的倍數 — 那是買點 → T1 的空間 vs 風險比
                rr = round((t1 - buy) / (buy - stop), 2) if (buy - stop) > 0 else None
                # 建議:買點現價差 < 1% 可進場,>3% 等回測;否則等待
                diff_pct = abs(price - buy) / buy * 100 if (price and buy) else None
                if diff_pct is not None:
                    if diff_pct <= 1 and price > (stop or 0):
                        advice = "可進場"
                    elif diff_pct <= 3:
                        advice = "等待回測"
                    else:
                        advice = "等待"
                # 沒停損資料 → 全等待
                if stop is None or buy is None:
                    advice = "等待"
                targets = {"advice": advice, "buy": buy, "stop": stop,
                           "t1": t1, "t2": t2, "rr": rr}
        except Exception as e:
            print(f"[detail] targets {code} 失敗:{e}")

        # 8) AI 結論 + 通過/未通過因子(來自 factors + 大戶/主動翻正/技術多頭/籌碼集中)
        ai_reasons = {"ai_score": None, "passes": [], "fails": []}
        try:
            ai_score = None
            factors = None
            for _s in (STATE.get("stocks") or []):
                if _s.get("code") == code:
                    ai_score = _s.get("ai_score")
                    factors = _s.get("factors")
                    break
            ai_reasons["ai_score"] = ai_score
            fc = factors or {}
            inst_net = chips_data.get("inst_net_20d_lots") or 0
            main_force = chips_data.get("main_force_net_20d") or 0
            big_holder_trend = chips_data.get("big_holder_trend") or 0
            aflow_ratio = health.get("aflow_ratio")
            # ✓ 大戶增加(千張趨勢 > 0)
            if big_holder_trend > 0:
                ai_reasons["passes"].append("大戶增加")
            elif big_holder_trend is not None:
                ai_reasons["fails"].append("大戶減少")
            # ✓ 主動資金翻正(盤中主動淨流比 > 0)
            if aflow_ratio is not None and aflow_ratio > 0:
                ai_reasons["passes"].append("主動資金翻正")
            elif aflow_ratio is not None:
                ai_reasons["fails"].append("主動資金偏空")
            # ✓ 技術多頭(MA5 > MA10 > MA20)
            ma5 = (indicators or {}).get("ma5")
            ma10 = (indicators or {}).get("ma10")
            ma20 = (indicators or {}).get("ma20")
            if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
                ai_reasons["passes"].append("技術多頭")
            elif ma5 is not None:
                ai_reasons["fails"].append("技術未多頭排列")
            # ✓/✕ 籌碼尚未完全集中(用千張 < 30% 或 400張趨勢 < 1pp 判斷)
            big_pct = chips_data.get("big_holder_pct")
            h400_trend = chips_data.get("holder_400_trend")
            if (big_pct is not None and big_pct < 30) or (h400_trend is not None and h400_trend < 1):
                ai_reasons["fails"].append("籌碼尚未完全集中")
            else:
                ai_reasons["passes"].append("籌碼集中")
            # 三大法人買超當加分
            if main_force > 0:
                ai_reasons["passes"].append("三大法人買超")
            elif main_force < 0:
                ai_reasons["fails"].append("三大法人賣超")
        except Exception as e:
            print(f"[detail] ai_reasons {code} 失敗:{e}")

        return _safe({
            "code": code,
            "snapshot": snap,
            "kbars": kbars,
            "chips": chips_data,
            "buy_sell": {
                "buy_volume_lots": round(bv / 1000, 1) if bv else 0,
                "sell_volume_lots": round(sv / 1000, 1) if sv else 0,
                "bs_pct": bs_pct,
                "active_buy_pct": active_buy_pct,
                "active_sell_pct": active_sell_pct,
            },
            "indicators": indicators,
            "health": health,
            "targets": targets,
            "ai_reasons": ai_reasons,
            "note": "五檔報價需 Shioaji tick stream,本機 API 不支援",
        })
    except Exception as e:
        traceback.print_exc()
        return _safe({"error": str(e)}, status_code=500)


def _safe(obj, status_code=200):
    """JSONResponse 包裝:用 jsonable_encoder 把 enum/datetime 轉成 JSON-safe 形式"""
    return JSONResponse(jsonable_encoder(obj), status_code=status_code)


if __name__ == "__main__":
    db.init()
    # scheduler_loop 已透過 lifespan 啟動(uvicorn/gunicorn 都會跑 lifespan)
    # 本地直接 python server.py 時也由 lifespan 啟動,避免重複
    uvicorn.run(app, host="0.0.0.0", port=8000)
