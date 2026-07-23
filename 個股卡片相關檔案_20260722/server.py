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

import multiprocessing
import threading
import time
import traceback
import json
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
import livermore
import decision_v22
import premarket
import daily_close_report
import engine_review
import money_health
import money_health_api

TW_TZ = timezone(timedelta(hours=8))

STATE = {"status": "starting"}
LOCK = threading.Lock()

# 盤前建立、盤中只讀的 MA20 快取；避免測試頁在盤中輪詢 kbars。
_ma20_cache = {}
_ma20_cache_date = ""
_ma20_cache_status = "未建立"

_watchlist_codes = set()
_pushed_lock_sectors = set()       # 今日已推播過鎖定的族群
_last_sector_snapshot = 0.0
_did_reverify = ""                 # 已執行開盤重驗的日期
_did_afterhours = ""               # 已執行盤後複查的日期
_did_eod_stamp = ""                # 已完成盤後歷史蓋章的日期
_last_full_state = None            # 收盤前最後一輪(供盤後複查)
LIVE_STATE = None                  # 最後一次有效盤中 live state
LIVE_STATE_UPDATE = 0.0            # LIVE_STATE 寫入時間
SHARED_STATE_PATH = Path(__file__).with_name("live_state.json")
_sig_watch = {}                    # code → {"stop":x, "failed":bool} 今日訊號追蹤
_consec_fails = 0                  # 連續停損計數(回撤斷路器)
_breaker_on = False                # True=當日停發新進場訊號

# === 資金健康度 記憶體快取(2026-07-16 Vanessa 要求「隨開隨用」) ===
# 第一次呼叫走完整運算(慢),結果存 _MH_CACHE;之後命中直接回。
# 失效策略:
#   - 盤中(週一~五 09:00~13:35):每 60 秒重算(資料會更新)
#   - 盤後 / 週末:不重算(收盤資料定)
#   - 手動清空:重啟 server 或觸發 /api/money_health?refresh=1
_mh_cache = {"payload": None, "ts": 0.0, "computing": False}
_MH_TTL_INTRADAY = 60.0            # 盤中快取壽命(秒)
_mh_lock = threading.Lock()       # 防止併發重算


def _now():
    return datetime.now(TW_TZ)


def _hm():
    return _now().strftime("%H:%M")


def _is_trade_day():
    return _now().weekday() < 5


def _live_session_open():
    """盤中主頁應優先顯示即時行情的時間窗。"""
    hm = _hm()
    return _is_trade_day() and "09:00" <= hm <= "13:35"


def load_today_watchlist():
    global _watchlist_codes
    wl = db.load_watchlist(db.today())
    _watchlist_codes = {w["stock_id"] for w in wl if not w.get("demoted")}
    if wl:
        print(f"[server] 今日觀察清單 {len(wl)} 檔(有效 {len(_watchlist_codes)})")


def refresh_ma20_cache(today=None):
    """盤前一次性建立 MA20；失敗個股留 None，不影響主行情服務。"""
    global _ma20_cache, _ma20_cache_date, _ma20_cache_status
    import broker
    from mls_intraday import prefetch_ma20

    universe = set(getattr(C, "SECTOR_MAP", {}).keys())
    universe.update(getattr(C, "ENGINE_STOCKS", set()))
    universe = sorted(str(code) for code in universe)
    if not universe:
        _ma20_cache_status = "無股票清單"
        return
    try:
        cache = prefetch_ma20.build_ma20_cache(broker.get_api(), universe)
        _ma20_cache = cache
        _ma20_cache_date = today or db.today()
        ready = sum(value is not None for value in cache.values())
        _ma20_cache_status = f"已建立 {ready}/{len(cache)} 檔"
        print(f"[server] MA20 快取 {_ma20_cache_status}，盤中只讀")
    except Exception as exc:
        _ma20_cache_status = f"建立失敗: {exc}"
        print(f"[server] MA20 快取建立失敗: {exc}")


def get_ma20(code):
    """盤中只讀 MA20；不在此處呼叫行情 API。"""
    return _ma20_cache.get(str(code))


def ma20_cache_status():
    return {"date": _ma20_cache_date, "status": _ma20_cache_status,
            "ready": sum(value is not None for value in _ma20_cache.values()),
            "total": len(_ma20_cache)}


def _write_shared_state(state):
    """讓獨立行情程序的最新快照可被 HTTP 程序讀取。"""
    try:
        payload = {k: v for k, v in (state or {}).items()
                   if not str(k).startswith("_")}
        tmp = SHARED_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(SHARED_STATE_PATH)
    except Exception as exc:
        print(f"[server] shared state 寫入失敗: {exc}")


def _read_shared_state():
    """讀取行情程序最後一份快照；讀取失敗不影響 API。"""
    try:
        if not SHARED_STATE_PATH.exists():
            return None
        return json.loads(SHARED_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[server] shared state 讀取失敗: {exc}")
        return None


def stamp_intraday_eod(state, today):
    """用最後一輪有效盤中快照蓋章；只寫獨立 intraday_eod.db。"""
    from mls_intraday import eod_stamp, intraday_filter as F

    raw_snaps = state.get("_snaps") or []
    if not raw_snaps:
        raise RuntimeError("沒有最後一輪有效盤中快照")
    snaps = []
    for raw in raw_snaps:
        buy = int(raw.get("buy_volume") or 0)
        sell = int(raw.get("sell_volume") or 0)
        code = str(raw.get("code", ""))
        snaps.append(F.StockSnap(
            code=code,
            track="engine" if code in getattr(C, "ENGINE_STOCKS", set()) else "attack",
            price=float(raw.get("price") or 0),
            change_rate=float(raw.get("change_rate") or 0),
            # broker 的 buy/sell 已正規化為主動買/賣，維持 ask - bid。
            aflow=F.aflow_official(sell, buy),
            total_volume=int(raw.get("total_volume") or 0),
            ma20=get_ma20(code),
        ))
    score = int((state.get("market") or {}).get("score") or 50)
    db_path = str(Path(__file__).with_name("intraday_eod.db"))
    result = eod_stamp.run_eod_stamp(db_path, snaps, score, trade_date=today)
    print(f"[server] ✅ 盤後歷史蓋章: {result}")
    return result


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
    global STATE, _did_reverify, _did_afterhours, _did_eod_stamp, \
           _last_sector_snapshot, _last_full_state, _pushed_lock_sectors, \
           LIVE_STATE, LIVE_STATE_UPDATE

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
                if _ma20_cache_date != today:
                    refresh_ma20_cache(today)
                if _did_reverify != today and hm >= "08:55":
                    import scoring
                    scoring.reset_aflow()        # 每日開盤重置主動淨流
                    scoring.reset_bs()           # 每日開盤重置BS濾網近端估算
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
                # 開盤初期 broker 容易回空(< 30 檔),走 eod_state 兜底避免整頁空白
                # 等下一輪 broker 拿到足夠資料自動切回真實 STATE
                _snaps = state.get("_snaps") or []
                if len(_snaps) < 30 and not state.get("sectors"):
                    try:
                        import eod_state
                        state = eod_state.build()
                        # BUG-1 修補(2026-07-15):同上,eod_state 官方價覆蓋時
                        # 沒重算 suggested_stop,補丁放這層避免動規則 22 鎖定檔。
                        for st in (state.get("stocks") or []):
                            _ap = st.get("avg_price") or 0
                            _lw = st.get("low") or 0
                            if _ap or _lw:
                                st["suggested_stop"] = round(
                                    max(_ap * 0.985 if _ap else 0, _lw), 2)
                            else:
                                st["suggested_stop"] = None
                        print(f"[server] 盤中 broker 回 {_snaps}/{len(_snaps)} 檔,eod_state 兜底")
                    except Exception as _e:
                        print(f"[server] eod_state 兜底失敗,沿用 broker 空 state:{_e}")
                # ── [最後防線] broker 空且 eod_state 也失敗時,沿用上一輪成功的 STATE ──
                # 避免空 state 覆蓋掉畫面(加權指數→—、觀察池→0、熱力圖全空)。
                # 判定:本輪 sectors 為空但上一輪有 → 保留上一輪,標 stale 讓前端知道是舊資料。
                if not (state.get("_sectors_full") or state.get("sectors")):
                    if _last_full_state and (_last_full_state.get("_sectors_full")
                                             or _last_full_state.get("sectors")):
                        print("[server] 本輪資料空,沿用上一輪 STATE(stale)")
                        state = dict(_last_full_state)
                        state["stale"] = True
                # 最後一哩保護：broker 重連時可能短暫產生空輪或 EOD fallback。
                # 只有「即時來源 + raw buffer + 個股」才可更新 LIVE_STATE；
                # 無效輪次在 5 分鐘內沿用上一輪成功的 live state。
                raw_count = state.get("raw_count") or 0
                stocks_count = len(state.get("stocks") or [])
                is_live = (
                    state.get("source") == "Shioaji realtime + FinMind chips(EOD)"
                    and raw_count > 0
                    and stocks_count > 0
                )
                if is_live:
                    LIVE_STATE = dict(state)
                    LIVE_STATE_UPDATE = time.time()
                    print(f"[scheduler] ✅ 寫入 live state: stocks={stocks_count}, raw={raw_count}")
                elif LIVE_STATE and (time.time() - LIVE_STATE_UPDATE) < 300:
                    state = dict(LIVE_STATE)
                    state["stale"] = True
                    print(f"[scheduler] ⏳ 沿用上一輪 live state (stocks={len(state.get('stocks') or [])})，本輪 raw_count={raw_count}")
                _last_full_state = state
                if hm >= "13:30" and _did_eod_stamp != today:
                    try:
                        stamp_intraday_eod(state, today)
                        _did_eod_stamp = today
                    except Exception as exc:
                        print(f"[server] 盤後歷史蓋章失敗，下一輪重試: {exc}")
                check_stops(state)
                handle_new_signals(state)
                handle_sector_locks(state)
                if time.time() - _last_sector_snapshot >= 300:   # 每5分鐘
                    sector_rows = state.get("_sectors_full") or state.get("sectors") or []
                    if sector_rows:
                        db.insert_sector_snapshot(sector_rows)
                        _last_sector_snapshot = time.time()
                # ── 注入健康度 v3(凍結版 403773c):給前端 💰 資金健康度 tab 用
                try:
                    money_health.annotate(state.get("_snaps", []),
                                          state.get("_sectors_full", []))
                except Exception as e:
                    print(f"[money_health] 盤中注入失敗:{e}")
                with LOCK:
                    STATE = {k: v for k, v in state.items()
                             if not k.startswith("_")}
                _write_shared_state(STATE)
                print(f"[scheduler] 寫入 STATE stocks={len(STATE.get('stocks') or [])} sectors={len(STATE.get('sectors') or [])} source={STATE.get('source')} updated_at={STATE.get('updated_at')}")
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
                        try:
                            money_health.annotate(snaps, secs)
                        except Exception as e:
                            print(f"[money_health] EOD 兜底注入失敗:{e}")
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
            # 收盤後 broker 拿不到 snaps,改走 eod_state.build()
            # 從 mls.db 真實落地的 health_daily / sector_daily 組 STATE,
            # 首頁熱力圖/資金流動/盤面速覽全亮起來。
            try:
                import eod_state
                state = eod_state.build()
                # BUG-1 修補(2026-07-15):eod_state 官方價覆蓋 stock 時沒重算
                # suggested_stop,會用 avg=100 的舊價算出 106 之類的離譜值。
                # 這裡對齊 engine.py:340-342 的 stop 公式重算
                # (max(avgp*0.985, low))。規則 22 鎖定 eod_state.py 不動,
                # 補丁放 server.py。
                for st in (state.get("stocks") or []):
                    avgp = st.get("avg_price") or 0
                    low = st.get("low") or 0
                    if avgp or low:
                        st["suggested_stop"] = round(
                            max(avgp * 0.985 if avgp else 0, low), 2)
                    else:
                        st["suggested_stop"] = None
            except Exception as _e:
                print(f"[server] eod_state 組裝失敗,fallback engine.build_state:{_e}")
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
# 盤中隔離測試頁：只讀既有 broker buffer，不另開行情連線。
import sys as _sys, pathlib as _pl, os as _os
_INTRADAY_ROOT = _pl.Path(__file__).resolve().parent.parent
if str(_INTRADAY_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_INTRADAY_ROOT))
try:
    import vps_intraday_test
    app.include_router(vps_intraday_test.router)
except Exception as e:
    print(f"[intraday-test] disabled: {e}")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── 插件掛載:李佛摩價格紀錄 / MLS 資金決策 v2.2(失敗不影響主系統) ──
try:
    app.include_router(livermore.router)
except Exception as e:
    print(f"[plugin/livermore] router 掛載失敗:{e}")
try:
    if decision_v22.router is not None:
        if hasattr(decision_v22, "set_state_provider"):
            decision_v22.set_state_provider(lambda: _last_full_state)
        app.include_router(decision_v22.router)
except Exception as e:
    print(f"[plugin/decision] router 掛載失敗:{e}")
try:
    if premarket.router is not None:
        app.include_router(premarket.router)
except Exception as e:
    print(f"[plugin/premarket] router 掛載失敗:{e}")
try:
    if daily_close_report.router is not None:
        app.include_router(daily_close_report.router)
except Exception as e:
    print(f"[plugin/daily_close_report] router 掛載失敗:{e}")
try:
  if engine_review.router is not None:
        app.include_router(engine_review.router)
except Exception as e:
    print(f"[plugin/engine_review] router 掛載失敗:{e}")
try:
    if money_health_api.router is not None:
        app.include_router(money_health_api.router)
except Exception as e:
    print(f"[plugin/money_health_api] router 掛載失敗:{e}")


@app.get("/money-health")
def money_health_page():
    """新版資金健康度證據卡頁面。"""
    html = Path(__file__).with_name("money_health_v23.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/money_health")
def api_money_health(refresh: int = 0):
    """資金健康度 v3(403773c freeze):50 檔個股 _health + 四象限 verdict 統計。
    來源:STATE._snaps(盤中)或即時 build_state(非盤中時)

    快取策略(2026-07-16 Vanessa 要求隨開隨用):
      - 盤中(週一~五 09:00~13:35):每 60 秒重算一次
      - 盤後/週末:算一次就不重算(資料定)
      - query ?refresh=1 強制重算(debug 用)"""
    # 1) 判斷現在是否盤中 + 快取是否新鮮
    _now_tw = _now()
    _is_intraday = (_now_tw.weekday() < 5
                    and "09:00" <= _now_tw.strftime("%H:%M") <= "13:35")
    _ttl = _MH_TTL_INTRADAY if _is_intraday else None  # 盤後不過期(None)
    with _mh_lock:
        _cached = _mh_cache.get("payload")
        _age = time.time() - _mh_cache.get("ts", 0.0)
        _fresh = (_cached is not None
                  and (_ttl is None or _age < _ttl)
                  and not refresh)
        if _fresh:
            return JSONResponse(_cached)
    # 2) 不新鮮 → 算
    payload = _api_money_health_compute()
    with _mh_lock:
        _mh_cache["payload"] = payload
        _mh_cache["ts"] = time.time()
    return JSONResponse(payload)


def _api_money_health_compute():
    """實際運算邏輯(原 api_money_health 內文,純函式版回傳 dict 而非 Response)。"""
    with LOCK:
        snaps = list(STATE.get("_snaps") or [])
        sectors = list(STATE.get("_sectors_full") or [])
    src = "live"
    if not snaps:
        _now_tw = _now()
        _mkt = _now_tw.weekday() < 5 and "09:00" <= _now_tw.strftime("%H:%M") <= "13:35"
        if _mkt:                                    # 只在交易時段打 Shioaji;盤後直接走 EOD(避免 13s 空等)
            try:
                state = engine.build_state(watchlist_codes=_watchlist_codes)
                snaps = state.get("_snaps", [])
                sectors = state.get("_sectors_full", [])
            except Exception:
                snaps = []
    if not snaps:
        # 盤後 Shioaji 回 0 → 改用 FinMind 收盤快照(EOD),資金腳中性、其餘為真
        try:
            import eod_source
            est = eod_source.eod_state()
            snaps = est.get("_snaps", [])
            sectors = est.get("_sectors_full", [])
            src = "eod"
        except Exception as e:
            return {"error": f"資料源皆失敗:{e}",
                    "items": [], "verdict_counts": {}, "source": "none",
                    "as_of": _now().isoformat()}
    try:
        annotated, verdict_counts = money_health.annotate(snaps, sectors)
        # v2.2 annotate 回傳新列表;舊版則原地寫入 _health 並回傳 sector map。
        # 兩種介面都支援,避免健康度已算出卻被路由丟成 items=[]。
        if isinstance(annotated, list):
            health_rows, sec_map = annotated, []
        else:
            health_rows, sec_map = snaps, annotated
    except Exception as e:
        return {"error": f"annotate 失敗:{e}",
                "items": [], "verdict_counts": {},
                "source": src, "as_of": _now().isoformat()}
    # 時間序列腳:補資金連續天數 flow_streak / 健康趨勢 health_trend
    try:
        import health_timeseries
        health_timeseries.enrich_and_persist(snaps)
    except Exception:
        pass
    sec_pct = {x.get("name"): x.get("pct") for x in (sectors or [])}
    # BUG-3 修補(2026-07-15):snaps 內個股沒附 _chip,從 STATE.stocks[].chip
    # 反查注入,讓資金健康度頁籌碼欄位不再是 None/—。盤後 STATE 為空時,
    # 改用 eod_state.build() 拿完整 stocks(含 chip) 建索引。
    _chip_idx = {}
    with LOCK:
        _state_stocks = (STATE.get("stocks") or []) if STATE else []
    for st in _state_stocks:
        c = st.get("chip")
        if c and st.get("code"):
            _chip_idx[st["code"]] = c
    if not _chip_idx and src == "eod":
        try:
            import eod_state
            for st in (eod_state.build().get("stocks") or []):
                c = st.get("chip")
                if c and st.get("code"):
                    _chip_idx[st["code"]] = c
        except Exception:
            pass
    items = []
    for s in health_rows:
        h = s.get("_health") or s
        if not h:
            continue
        chip = s.get("_chip") or _chip_idx.get(s.get("code"), {}) or {}
        chg = s.get("change_rate")
        spct = sec_pct.get(s.get("sector"))
        module_scores = h.get("module_scores") or {
            "price": h.get("price_score"),
            "flow": h.get("money_score"),
            "chip": h.get("chip_score"),
            "sector": h.get("sector_score"),
        }
        items.append({
            "code": s.get("code"),
            # 健康度頁也必須沿用固定名稱表；部分即時 snap 沒有 name
            # 時不可把 null 直接送到前端。
            "name": (s.get("name")
                     or C.NAME_MAP.get(str(s.get("code")), str(s.get("code")))),
            "sector": s.get("sector") or h.get("sector_name") or s.get("sector_name"),
            "score": h.get("health_score"),
            "quadrant": h.get("quadrant"),
            "label": h.get("label"),
            "desc": h.get("desc"),
            "stars": h.get("stars"),
            "module_scores": module_scores,
            "chip_quality": h.get("chip_quality"),
            "change_rate": chg,
            "sector_pct": spct,
            "rs_vs_sector": (round(chg - spct, 2) if isinstance(chg, (int, float))
                             and isinstance(spct, (int, float)) else None),
            "aflow_ratio": h.get("aflow_ratio"),
            "flow_streak": h.get("flow_streak"),
            "health_trend": h.get("health_trend"),
            "inst_net_20d_lots": chip.get("inst_net_20d_lots"),
            "inst_streak": chip.get("inst_streak"),
            "big_holder_pct": chip.get("big_holder_pct"),
            "big_holder_trend": chip.get("big_holder_trend"),
            "price": s.get("price"),
            "volume_ratio": s.get("volume_ratio", s.get("tnvr")),
            "tnvr": s.get("tnvr"),
        })
    items.sort(key=lambda x: -(x.get("score") or 0))
    return json.loads(json.dumps({
        "items": items,
        "verdict_counts": verdict_counts,
        "sector_health": sec_map,
        "source": src,
        "as_of": _now().isoformat(),
    }, default=str, ensure_ascii=False))


# (2026-07-16 mavis 嘗試加 /api/mh/overview 但 server 11308 已用 Vanessa 的 money_health_api.overview() 處理,移除以免 reload 衝突)
_funnel_eod_done = set()


@app.get("/api/funnel")
def api_funnel(date: str = None):
    """四關漏斗兩梯隊(盤後產出)。首頁『明日觀察』短名單讀這支。
    🔴第一梯隊=四關全過;🟡第二梯隊=過三關差一關;零檔誠實顯零。
    盤後若表內尚無今日結果 → 用 FinMind EOD 產一次(當日僅一次)。"""
    try:
        import funnel
        try:
            res = funnel.latest(date)
        except AttributeError:
            res = {"date": None, "tier1": [], "tier2": [], "note": "funnel.latest 未實作(funnel 模組鎖死)"}
            return JSONResponse(res)
        need = (not res.get("tier1")) and (not res.get("tier2"))
        if need and date is None:
            import eod_source
            today = eod_source._today()
            if today not in _funnel_eod_done:
                _funnel_eod_done.add(today)
                try:
                    import after_hours, money_health, health_timeseries
                    est = eod_source.eod_state()
                    snaps, sectors = est["_snaps"], est["_sectors_full"]
                    if snaps:
                        money_health.annotate(snaps, sectors)
                        try:
                            health_timeseries.enrich_and_persist(snaps)
                        except Exception:
                            pass
                        rotation, resilient = after_hours.rotation_analysis(sectors, snaps)
                        wl = after_hours.build_tomorrow_watchlist(sectors, snaps)
                        funnel.run(est, rotation_reports=rotation, resilient=resilient,
                                   watchlist=wl, sixpoint=None)
                        res = funnel.latest()
                        res["source"] = "eod"
                except Exception as e:
                    res["note"] = (res.get("note") or "") + f"(EOD 產生失敗:{e})"
        return JSONResponse(json.loads(json.dumps(res, default=str, ensure_ascii=False)))
    except Exception as e:
        return JSONResponse({"date": None, "tier1": [], "tier2": [],
                             "note": f"漏斗讀取失敗:{e}"})


@app.get("/api/market/official")
def api_market_official():
    """官方三大法人買賣超 + 大盤(給首頁 banner;有官方不自算)。"""
    try:
        import official_source as o
        return JSONResponse(json.loads(json.dumps({
            "institutional": o.institutional_net(),
            "index": o.market_index(),
        }, default=str, ensure_ascii=False)))
    except Exception as e:
        return JSONResponse({"error": f"官方源讀取失敗:{e}"})


@app.get("/healthz")
async def healthz():
    """容器健康檢查只確認 HTTP event loop 活著，不讀 Shioaji 狀態。"""
    return {"ok": True, "service": "mls", "status": "running"}


@app.get("/api/quota")
async def api_quota():
    """回報 Shioaji 流量額度(從 server 自己已登入的 instance 讀,不開新 process)。"""
    import broker
    try:
        # 健康檢查不可在 HTTP request 內觸發登入／重連；只讀既有 instance。
        api = getattr(broker, "_api", None)
        if api is None:
            return {"used_mb": None, "limit_mb": None, "pct": None,
                    "subscribed": len(broker._SUBSCRIBED),
                    "buffer_filled": len(broker._QUOTE_BUF),
                    "status": "not_logged_in"}
        # usage() 也可能等待 Shioaji 網路回應；健康檢查只需確認 process
        # 活著與行情緩衝，不在這裡查額度。
        return {"used_mb": None, "limit_mb": None, "pct": None,
                "subscribed": len(broker._SUBSCRIBED),
                "buffer_filled": len(broker._QUOTE_BUF),
                "status": "logged_in"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/state")
def api_state():
    with LOCK:
        print("[api] ===== /api/state 被呼叫 =====")
        print(f"[api] STATE keys: {list(STATE.keys()) if STATE else 'None'}")
        print(f"[api] STATE.stocks 數量: {len(STATE.get('stocks') or []) if STATE else 0}")
        print(f"[api] STATE.sectors 數量: {len(STATE.get('sectors') or []) if STATE else 0}")
        print(f"[api] STATE.source: {STATE.get('source') if STATE else 'None'}")
        print(f"[api] STATE.updated_at: {STATE.get('updated_at') if STATE else 'None'}")
        print(f"[api] STATE.status: {STATE.get('status') if STATE else 'None'}")
        if STATE and STATE.get('stocks'):
            print(f"[api] 第一檔股票: {STATE['stocks'][0].get('code')}")
        print("[api] =================================")
        _shared = _read_shared_state()
        if _shared and (_shared.get("stocks") or _shared.get("sectors")):
            _shared["stale"] = False
            return JSONResponse(_shared)
        # 盤中即使 STATE 仍保留 EOD/官方資料，也必須優先回傳最近有效的
        # Shioaji live state；否則主頁會看起來像「行情沒通」或停在昨日。
        if (_live_session_open() and LIVE_STATE
                and (time.time() - LIVE_STATE_UPDATE) < 300):
            _live = dict(LIVE_STATE)
            _live["stale"] = False
            print(f"[api] ⏳ 回傳 LIVE_STATE stocks={len(_live.get('stocks') or [])}")
            return JSONResponse(_live)
        # 盤中不得在 HTTP request 內同步跑 eod_state.build()；該計算可能
        # 等待官方資料，會讓主頁整個卡住。等 scheduler 下一輪更新即可。
        if _live_session_open():
            _pending = dict(STATE or {"status": "starting"})
            _pending["live_pending"] = True
            return JSONResponse(_pending)
        # STATE 任何時候 sectors 或 stocks 全空 → 兜底 eod_state(mls.db 真實歷史)
        # 場景:盤中 broker 回空、scheduler 還沒跑、scheduler 跑出空
        if not STATE or STATE.get("status") in (None, "starting") \
                or (not STATE.get("sectors") and not STATE.get("stocks")):
            try:
                import eod_state
                _fallback = eod_state.build()
                # BUG-1 修補(2026-07-15):eod_state 官方價覆蓋 stock 時沒重算
                # suggested_stop,這層在 server.py 補丁,避免動規則 22 鎖定檔。
                for st in (_fallback.get("stocks") or []):
                    _ap = st.get("avg_price") or 0
                    _lw = st.get("low") or 0
                    if _ap or _lw:
                        st["suggested_stop"] = round(
                            max(_ap * 0.985 if _ap else 0, _lw), 2)
                    else:
                        st["suggested_stop"] = None
                return JSONResponse(json.loads(json.dumps(
                    _fallback, default=str, ensure_ascii=False)))
            except Exception as e:
                return JSONResponse({"error": f"eod_state 兜底失敗:{e}",
                                    "status": "starting"})
        return JSONResponse(STATE)


@app.get("/api/review")
def api_review():
    """近30日命中率 + 今日統計(前端學習區/複盤頁用)"""
    return JSONResponse({
        "recent_hit_rates": db.recent_hit_rates(30),
        "today": db.today_stats(),
        "watchlist_today": db.load_watchlist(db.today()),
    })


@app.get("/api/review/stocks")
def api_review_stocks(trade_date: str = ""):
    """盤後驗證逐檔資料：觀察清單合併該日最後一筆訊號。"""
    day = trade_date or db.today()
    with db._lock, db._conn() as c:
        watch = [dict(r) for r in c.execute(
            "SELECT * FROM watchlist WHERE trade_date=? ORDER BY stock_id", (day,))]
        latest = {}
        for r in c.execute(
            """SELECT s.* FROM signals s
               JOIN (SELECT stock_id, MAX(id) id FROM signals
                     WHERE trade_date=? GROUP BY stock_id) x ON x.id=s.id""", (day,)):
            latest[r["stock_id"]] = dict(r)
    rows = []
    for w in watch:
        s = latest.get(w["stock_id"], {})
        rows.append({
            "trade_date": day, "code": w["stock_id"],
            "name": w.get("stock_name") or s.get("stock_name") or w["stock_id"],
            "sector": w.get("sector"), "reason": w.get("reason"),
            "reverified": bool(w.get("reverified")),
            "demoted": bool(w.get("demoted")), "hit": bool(w.get("hit")),
            "action": s.get("action"), "event_class": s.get("event_class"),
            "price": s.get("price"), "change_rate": s.get("change_rate"),
            "volume_ratio": s.get("volume_ratio"),
            "suggested_stop": s.get("suggested_stop"),
            "confidence_label": s.get("confidence_label"),
            "signal_ts": s.get("ts"), "signal_count": sum(
                1 for _ in [s] if s),
        })
    return {"ok": True, "trade_date": day, "rows": rows,
            "note": "價格與訊號取該日最後一筆；命中取 watchlist 收盤驗證欄位"}


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


@app.get("/")
def home():
    html = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


# ══════════════════════════════════════════════════════════
# 個股卡片 / 每日報告 / 51 檔觀察池 — extras 模組接的路由
# ══════════════════════════════════════════════════════════
import extras as _extras  # noqa: E402


@app.get("/card")
def stock_card_page():
    """個股決策卡片 UI（依 query ?code=xxxx 顯示單檔）。"""
    return HTMLResponse(_read_html("deepseek_stock card.html"),
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/stock/{code}")
def api_stock(code: str):
    """單檔個股決策卡 — 接 stock_card.build_card() + VPS Shioaji snap。"""
    try:
        return JSONResponse(_extras.build_stock_card(code))
    except Exception as exc:
        return JSONResponse({"ok": False, "code": code, "error": str(exc)}, status_code=500)


def _read_html(filename: str) -> str:
    """中文檔名安全的 HTML 讀取 — Path.with_name 在某些 locale 會壞。"""
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), filename)
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/report")
def report_page():
    """每日報告 UI（盤後驗證摘要）。"""
    return HTMLResponse(_read_html("每日報告.html"),
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/report")
def api_report():
    """每日 / 昨日盤後報告資料。"""
    try:
        return JSONResponse(_extras.build_report())
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/watchpool")
def watchpool_page():
    """51 檔觀察池 UI。"""
    return HTMLResponse(_read_html("nexora_watchpool_51_standalone.html"),
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/watchpool")
def api_watchpool():
    """51 檔觀察池全集 — 從 VPS Shioaji 訂閱 buffer 抓即時報價。"""
    try:
        return JSONResponse(_extras.build_watchpool())
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


if __name__ == "__main__":
    db.init()
    # 行情排程獨立程序執行；Shioaji／資料抓取即使阻塞，也不能卡住 HTTP。
    def _start_scheduler_delayed():
        time.sleep(3)
        scheduler_loop()
    multiprocessing.Process(target=_start_scheduler_delayed, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000)
