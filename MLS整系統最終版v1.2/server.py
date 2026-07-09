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
                    state = _last_full_state or {"sectors": [], "stocks": [], "locked_sectors": [], "leaders": [], "market": {"index": 0, "index_pct": 0, "amount_100m": 0, "score": 0, "mode": "—", "time": hm}, "is_market_hours": False}
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


@app.get("/preview_v23")
def preview_v23():
    """v2.3 UI preview with mocked state (只用於截圖驗收,看 UI 是否你喜歡)。"""
    html = Path(__file__).with_name("preview_v23.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/v23_mock_design")
def v23_mock_design():
    """v2.3 靜態設計稿(個股卡片第二層 + 頂部 banner 純展示)。"""
    html = Path(__file__).with_name("v23_mock_design.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


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
      chips       法人/大戶(來自 chips.get_chips)
      buy_sell    內外盤累計量 + BS Ratio
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
            chips_data = {"has_data": False, "inst_net_20d_lots": None, "inst_streak": None, "big_holder_pct": None, "big_holder_trend": None}

        # 4) buy_sell BS Ratio
        bv = snap.get("buy_volume") or 0
        sv = snap.get("sell_volume") or 0
        bs_pct = None
        if bv + sv > 0:
            bs_pct = round(bv / (bv + sv) * 100, 1)

        return _safe({
            "code": code,
            "snapshot": snap,
            "kbars": kbars,
            "chips": chips_data,
            "buy_sell": {
                "buy_volume_lots": round(bv / 1000, 1) if bv else 0,
                "sell_volume_lots": round(sv / 1000, 1) if sv else 0,
                "bs_pct": bs_pct,
            },
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
