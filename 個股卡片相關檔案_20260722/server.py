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
import explain  # 說明語意層:後台算白話,前台只印(見 說明語意層規格.md)
import intraday_note  # 淘汰名單「今日盤中說明」:淘汰理由 × 今日盤中資金流/漲跌 → 背離/確認

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
INTRADAY_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "intraday_live_snapshot.json"


def _persist_intraday_snapshot(state, trade_date):
    """盤中主迴圈直接保存最後有效快照，和盤後篩選結果分離。"""
    try:
        raw = list(state.get("_snaps") or [])
        if not raw:
            return
        import vps_intraday_test as _vit
        rows = [_vit._row(item) for item in raw]
        rows = [row for row in rows if row.get("code") and row.get("price") is not None]
        if not rows:
            return
        groups = {}
        for row in rows:
            groups[row["group"]] = groups.get(row["group"], 0) + 1
        result = {
            "ok": True,
            "source": "VPS scheduler persisted last intraday state",
            "read_only": True,
            "updated_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
            "trade_date": trade_date,
            "count": len(rows),
            "rows": rows,
            "category_counts": groups,
            "snapshot": True,
            "notes": ["盤中累積資料收盤後凍結；與盤後籌碼篩選分離，不歸零"],
        }
        payload = {"trade_date": trade_date,
                   "saved_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
                   "result": result}
        tmp = INTRADAY_SNAPSHOT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str, ensure_ascii=False), encoding="utf-8")
        tmp.replace(INTRADAY_SNAPSHOT_PATH)
        print(f"[snapshot] ✅ 保存盤中凍結快照 rows={len(rows)} date={trade_date}", flush=True)
    except Exception as exc:
        print(f"[snapshot] 保存盤中凍結快照失敗: {exc}", flush=True)
SHARED_STATE_PATH = Path(__file__).with_name("live_state.json")
MA20_CACHE_PATH = Path(__file__).with_name("ma20_cache.json")
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


def _eod_state_latest():
    """eod_state.build() 今日無落地資料時，自動退到資料庫最近一個交易日。

    規範:「就算當日還沒更新，也應該採用最新數據，而不是空白」。
    eod_state.py 是規則 22 鎖定檔，回退補丁一律放 server.py。"""
    import eod_state
    st = eod_state.build()
    if st.get("stocks") or st.get("sectors"):
        return st
    try:
        latest = None
        with db._lock, db._conn() as c:
            # health_daily 的寫入模組不存在，實際落地的是
            # sector_snapshot(盤中每5分)與 sector_daily(盤後)。逐表找最近日。
            for table in ("sector_snapshot", "sector_daily", "health_daily"):
                try:
                    r = c.execute(
                        f"SELECT MAX(trade_date) d FROM {table}").fetchone()
                    if r and r["d"]:
                        latest = max(latest, r["d"]) if latest else r["d"]
                except Exception:
                    continue
        if latest:
            st2 = eod_state.build(date=latest)
            if st2.get("stocks") or st2.get("sectors"):
                st2 = dict(st2)
                st2["data_date"] = latest
                st2["latest_fallback"] = True
                return st2
    except Exception as e:
        print(f"[server] eod_state 最新日回退失敗:{e}")
    return st


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
    try:
        from mls_intraday import prefetch_ma20
    except ImportError:
        from app import prefetch_ma20

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
        MA20_CACHE_PATH.write_text(json.dumps({
            "date": _ma20_cache_date,
            "cache": cache,
            "status": _ma20_cache_status,
        }, ensure_ascii=False), encoding="utf-8")
        print(f"[server] MA20 快取 {_ma20_cache_status}，盤中只讀")
    except Exception as exc:
        _ma20_cache_status = f"建立失敗: {exc}"
        print(f"[server] MA20 快取建立失敗: {exc}")


def get_ma20(code):
    """盤中只讀 MA20；不在此處呼叫行情 API。"""
    if not _ma20_cache and MA20_CACHE_PATH.exists():
        try:
            saved = json.loads(MA20_CACHE_PATH.read_text(encoding="utf-8"))
            _ma20_cache.update(saved.get("cache") or {})
            globals()["_ma20_cache_date"] = saved.get("date", "")
            globals()["_ma20_cache_status"] = saved.get("status", "已載入共享快取")
        except Exception as exc:
            print(f"[server] MA20 共享快取讀取失敗: {exc}")
    return _ma20_cache.get(str(code))


def ma20_cache_status():
    if not _ma20_cache and MA20_CACHE_PATH.exists():
        get_ma20("__status_probe__")
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



def _live_rows_map():
    """今日盤中每檔最新判讀（buffer 優先，收盤後用凍結快照），供盯盤名單即時更新。"""
    rows = {}
    try:
        import vps_intraday_test as _vit
        raw = None
        try:
            import broker as _bk
            raw = _bk.raw_buffer_snapshots()
        except Exception:
            raw = None
        if raw:
            for item in raw:
                r = _vit._row(item)
                if r.get("code"):
                    rows[str(r["code"])] = r
        else:
            saved = _vit._read_intraday_snapshot(allow_prev_day=True) or {}
            for r in (saved.get("rows") or []):
                if r.get("code"):
                    rows[str(r["code"])] = r
    except Exception as exc:
        print(f"[watch] 盤中判讀讀取失敗:{exc}")
    return rows


def _watch_verdict(live):
    """盯盤狀態：名單是昨晚選的，今天盤中表現是否兌現。"""
    if not live:
        return "待回報", "尚未收到盤中行情"
    g = live.get("group")
    chg = live.get("change_rate")
    aflow = live.get("aflow")
    if g == "可操作":
        return "兌現", f"盤中升級為可操作（{chg:+.2f}%）"
    if g == "排除":
        return "反向", f"盤中觸發風險訊號（{chg:+.2f}%），不宜進場"
    if chg is None:
        return "觀察中", "等待盤中行情"
    if chg >= 1.5 and (aflow or 0) >= 0:
        return "兌現", f"漲幅 {chg:+.2f}% 且資金流入，符合入選預期"
    if chg <= -1.5:
        return "反向", f"跌幅 {chg:+.2f}%，與入選預期背離"
    if (aflow or 0) < 0 and chg > 0:
        return "留意", f"上漲 {chg:+.2f}% 但資金流出，留意假紅"
    return "觀察中", f"{chg:+.2f}%，尚未走出方向"


def stamp_watch_outcome(today):
    """收盤把今日盯盤名單的實際結果寫入 watch_outcome 歷史。"""
    wl = db.load_watchlist(today)
    if not wl:
        print("[watch] 今日無盯盤名單，略過收盤蓋章")
        return 0
    live = _live_rows_map()
    rows = []
    for w in wl:
        code = str(w.get("stock_id"))
        lv = live.get(code) or {}
        verdict, note = _watch_verdict(lv)
        rows.append({
            "code": code, "name": w.get("stock_name"), "sector": w.get("sector"),
            "watch_reason": w.get("reason"),
            "open_group": lv.get("group"), "close_group": lv.get("group"),
            "close_price": lv.get("price"), "change_rate": lv.get("change_rate"),
            "aflow": lv.get("aflow"), "volume_ratio": lv.get("volume_ratio"),
            "verdict": verdict, "note": note,
        })
        if verdict == "兌現":
            try:
                db.mark_watch_hit(today, code)
            except Exception:
                pass
    db.save_watch_outcome(today, rows)
    hits = sum(1 for r in rows if r["verdict"] == "兌現")
    print(f"[watch] ✅ 收盤蓋章 {len(rows)} 檔，兌現 {hits} 檔 ({today})")
    return len(rows)



CHIPS_PREFETCH_DONE = ""


def prefetch_chips_cache(force=False):
    """盤前/盤後把 51 檔籌碼一次抓齊寫入 chips_cache.json。

    規範:盤中不打 FinMind。但法人連買/近月買超在前一日收盤即已定案，
    盤中理應直接讀得到,不該顯示「待補」。因此在盤前(08:30)與盤後(18:05)
    各建一次快取,盤中只讀檔案。
    """
    global CHIPS_PREFETCH_DONE
    today = db.today()
    if CHIPS_PREFETCH_DONE == today and not force:
        return 0
    codes = [str(c) for c in getattr(C, "UNIVERSE", [])]
    ok = 0
    # 官方優先：TWSE T86／TPEx 一次回全市場，免費無上限，51 檔只要 ~20 次請求。
    try:
        import chips_official
        ok = chips_official.build_cache(codes)
    except Exception as exc:
        print(f"[chips] 官方來源失敗，改用 FinMind 備援:{exc}")
    if ok < len(codes) * 0.6:
        # 官方covered 不足才動用 FinMind（免費額度有限，超量會回 402）
        try:
            import chips
            for code in codes:
                try:
                    r = chips.get_chips(code) or {}
                    if r.get("inst_streak") is not None:
                        ok += 1
                except Exception:
                    pass
                time.sleep(0.35)
        except Exception as exc:
            print(f"[chips] FinMind 備援失敗:{exc}")
    CHIPS_PREFETCH_DONE = today
    print(f"[chips] ✅ 籌碼快取建立 {ok}/{len(codes)} 檔（{today}）", flush=True)
    return ok


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

    print(f"[diag][scheduler] start ts={datetime.now().astimezone().isoformat(timespec='milliseconds')}", flush=True)
    load_today_watchlist()
    try:
        prefetch_chips_cache()       # 開機補一次，盤中才讀得到法人資料
    except Exception as exc:
        print(f"[chips] 開機預抓失敗:{exc}")
    if not _ma20_cache or _ma20_cache_date != db.today():
        print("[diag][scheduler] ma20_cache.bootstrap.begin", flush=True)
        refresh_ma20_cache(db.today())
        print("[diag][scheduler] ma20_cache.bootstrap.end", flush=True)

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
                if CHIPS_PREFETCH_DONE != today:
                    prefetch_chips_cache()
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
                _op_started = time.time()
                print(f"[diag][scheduler] build_state.begin hm={hm}", flush=True)
                state = engine.build_state(watchlist_codes=_watchlist_codes)
                print(f"[diag][scheduler] build_state.end elapsed_ms={round((time.time()-_op_started)*1000,1)} raw={len(state.get('_snaps') or [])}", flush=True)
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
                    _persist_intraday_snapshot(state, today)
                    print(f"[scheduler] ✅ 寫入 live state: stocks={stocks_count}, raw={raw_count}")
                elif LIVE_STATE and (time.time() - LIVE_STATE_UPDATE) < 300:
                    state = dict(LIVE_STATE)
                    state["stale"] = True
                    print(f"[scheduler] ⏳ 沿用上一輪 live state (stocks={len(state.get('stocks') or [])})，本輪 raw_count={raw_count}")
                _last_full_state = state
                if hm >= "13:30" and _did_eod_stamp != today:
                    try:
                        # 收盤瞬間 Shioaji buffer 可能已清空；必須使用最後一輪
                        # 有效盤中快照蓋章，不能把空 state 寫成盤後資料。
                        stamp_state = state
                        if not (stamp_state.get("_snaps") or stamp_state.get("stocks")):
                            stamp_state = LIVE_STATE or _last_full_state
                        stamp_intraday_eod(stamp_state, today)
                        try:
                            stamp_watch_outcome(today)
                        except Exception as exc:
                            print(f"[watch] 收盤蓋章失敗:{exc}")
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

            # ── 18:00 官方盤後資料完成後複查(一天一次;state 為空時兜底重抓) ──
            if hm >= "18:00" and _did_afterhours != today:
                state_for_eod = _last_full_state
                if state_for_eod is None:
                    # 服務若在收盤後才重啟，記憶體沒有盤中 state；
                    # 兜底順序:broker buffer(未重啟仍留最後值)→ 盤中快照檔。
                    print("[server] 盤中 state 缺失,EOD 兜底:讀最後盤中快照…")
                    try:
                        snaps = []
                        try:
                            import broker as _bk
                            snaps = _bk.raw_buffer_snapshots()
                        except Exception as e:
                            print(f"[server] 兜底 broker buffer 失敗:{e}")
                        if not snaps and INTRADAY_SNAPSHOT_PATH.exists():
                            _p = json.loads(INTRADAY_SNAPSHOT_PATH.read_text(encoding="utf-8"))
                            if _p.get("trade_date") == today:
                                snaps = ((_p.get("result") or {}).get("rows")) or []
                        if not snaps:
                            # 冷啟動最後一層:Shioaji 官方 snapshot 收盤後
                            # 仍回當日收盤值(51 檔、不佔訂閱額度)。
                            try:
                                import broker as _bk
                                snaps = _bk.batch_snapshots(
                                    [str(c) for c in C.UNIVERSE])
                                print(f"[server] 兜底改用官方收盤 snapshot {len(snaps)} 檔")
                            except Exception as e:
                                print(f"[server] 官方 snapshot 兜底失敗:{e}")
                        for s in snaps:
                            if not s.get("sector"):
                                _sec = C.SECTOR_MAP.get(str(s.get("code")))
                                s["sector"] = _sec[0] if _sec else "其他"
                        if snaps:
                            secs = engine.compute_sector_flow(snaps)
                            try:
                                money_health.annotate(snaps, secs)
                            except Exception as e:
                                print(f"[money_health] EOD 兜底注入失敗:{e}")
                            state_for_eod = {"_snaps": snaps,
                                             "_sectors_full": [
                                                 {k: v for k, v in s.items() if k != "members"}
                                                 for s in secs],
                                             "stocks": [], "sectors": []}
                        else:
                            print("[server] 兜底失敗:今日無任何盤中快照，明日觀察沿用最近名單")
                    except Exception as e:
                        print(f"[server] 兜底組裝失敗:{e}")
                try:
                    prefetch_chips_cache(force=True)   # 官方籌碼定案後重建快取
                except Exception as exc:
                    print(f"[chips] 盤後快取重建失敗:{exc}")
                if state_for_eod is not None:
                    _op_started = time.time()
                    print("[diag][scheduler] after_hours.begin", flush=True)
                    after_hours.run(state_for_eod)
                    print(f"[diag][scheduler] after_hours.end elapsed_ms={round((time.time()-_op_started)*1000,1)}", flush=True)
                    _did_afterhours = today
                time.sleep(60)
                continue

            # ── 非交易時段:輕量更新畫面 ───────────────
            # 收盤後 broker 拿不到 snaps,改走 eod_state.build()
            # 從 mls.db 真實落地的 health_daily / sector_daily 組 STATE,
            # 首頁熱力圖/資金流動/盤面速覽全亮起來。
            try:
                state = _eod_state_latest()
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
_scheduler_started = False


def _scheduler_worker():
    time.sleep(3)
    scheduler_loop()


def _launch_scheduler():
    """無論由 python server.py 或 uvicorn server:app 啟動，都只開一個排程。"""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    db.init()

    # 排程用「執行緒」而非「行程」：multiprocessing.Process 會另開一個子行程,
    # 該子行程 import broker 後對同一組金鑰再登入一次 Shioaji → 與 HTTP worker
    # 的登入互踢行情 session(SessionNotEstablished),盤中整個資金流/畫面死。
    # 改 Thread 共用同一個 process、同一個 broker 單例 = 全服務唯一 Shioaji 登入。
    # (2026-08-04 根治;build_state 為 IO 主、跑在 daemon thread 不阻塞 async HTTP)
    threading.Thread(target=_scheduler_worker, daemon=True, name="scheduler").start()
    print("[diag][scheduler] thread launched (single Shioaji login)", flush=True)


@app.on_event("startup")
async def _startup_scheduler():
    _launch_scheduler()
    _launch_card_prewarm()


def _launch_card_prewarm():
    """開機後把 51 檔個股卡片預先算好寫入快取。

    卡片吃盤後固定資料，同一交易日只需算一次；不預熱的話，重啟後第一個
    點進個股的人要等 Shioaji 登入＋日K 抓取（實測 ~40 秒）。這裡在背景
    逐檔慢慢跑（間隔 1 秒），已在快取中的直接跳過，不影響盤中訂閱。
    """
    def _worker():
        import time
        time.sleep(20)          # 讓行情訂閱先建立完成
        try:
            codes = list(getattr(C, "UNIVERSE", []) or [])
        except Exception:
            codes = []
        done = 0
        for code in codes:
            try:
                _extras.build_stock_card(str(code))
                done += 1
            except Exception as exc:
                print(f"[prewarm] {code} 失敗: {exc}", flush=True)
            time.sleep(1)
        print(f"[prewarm] 個股卡片預熱完成 {done}/{len(codes)}", flush=True)

    try:
        import threading
        threading.Thread(target=_worker, daemon=True, name="card-prewarm").start()
        print("[prewarm] 個股卡片預熱啟動", flush=True)
    except Exception as exc:
        print(f"[prewarm] 啟動失敗: {exc}", flush=True)
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
    """明日觀察清單(盤後 18:00 after_hours.run 產出，存 SQLite watchlist)。

    規則:指定日期 → 明日 → 今日 → 資料庫最近一次。永遠回最新可用
    名單並標明資料日，不回空白;真的一筆都沒有才回空(誠實顯零)。"""
    try:
        candidates = []
        if date:
            candidates.append(date)
        else:
            candidates.extend([after_hours.next_trade_date(), db.today()])
        rows, used_date, is_latest_fallback = [], None, False
        for day in candidates:
            rows = db.load_watchlist(day)
            if rows:
                used_date = day
                break
        if not rows:
            with db._lock, db._conn() as c:
                latest = c.execute(
                    "SELECT MAX(trade_date) d FROM watchlist").fetchone()
                latest_day = latest["d"] if latest else None
            if latest_day:
                rows = db.load_watchlist(latest_day)
                used_date = latest_day
                is_latest_fallback = True
        # 名單語意:watchlist 存的是「該交易日要盯的名單」，由前一晚 18:00
        # 盤後複查產出。因此同一份名單在今天叫「今日盯盤」、在昨晚叫「明日觀察」。
        _today, _next = db.today(), after_hours.next_trade_date()
        if used_date == _today:
            kind, tier_label = "today", "今日盯盤"
            title = "今日盯盤名單"
            subtitle = f"昨日 18:00 盤後籌碼定案後產出（名單日 {used_date}）；今晚 18:00 產出明日名單"
        elif used_date == _next:
            kind, tier_label = "tomorrow", "明日觀察"
            title = "明日觀察清單"
            subtitle = f"今日盤後籌碼已定案，明日（{used_date}）進場候選"
        else:
            kind, tier_label = "past", "歷史名單"
            title = f"{used_date} 名單"
            subtitle = "較新的盤後名單尚未產出，顯示最近一次可用名單"
        # 盯盤名單不是死資料：每列掛上今日盤中即時判讀與狀態。
        live_map = _live_rows_map() if kind == "today" else {}
        outcome = {o["stock_id"]: o for o in db.load_watch_outcome(used_date)}
        tier1 = []
        for r in rows:
            code = str(r.get("stock_id"))
            lv = live_map.get(code) or {}
            oc = outcome.get(code)
            if oc:
                verdict, vnote = oc.get("verdict"), oc.get("note")
            elif kind == "today":
                verdict, vnote = _watch_verdict(lv)
            else:
                verdict, vnote = "待盤中驗證", "此名單對應交易日尚未開盤"
            tier1.append({
                "code": code, "stock_id": code,
                "name": r.get("stock_name"), "stock_name": r.get("stock_name"),
                "sector": r.get("sector"), "reason": r.get("reason"),
                "tier": tier_label, "hit": bool(r.get("hit")),
                "demoted": bool(r.get("demoted")),
                "price": lv.get("price") or (oc or {}).get("close_price"),
                "change_rate": lv.get("change_rate") if lv else (oc or {}).get("change_rate"),
                "aflow": lv.get("aflow") if lv else (oc or {}).get("aflow"),
                "group": lv.get("group") or (oc or {}).get("close_group"),
                "quadrant": lv.get("quadrant"),
                "ai": lv.get("ai") or lv.get("classification_reason"),
                "verdict": verdict, "verdict_note": vnote,
                "stamped": bool(oc),
            })
        note = None
        if is_latest_fallback:
            note = f"較新名單尚未產出，顯示最近一次（{used_date}）"
        elif not rows:
            note = "資料庫尚無任何盤後名單；18:00 盤後複查完成後自動產出"
            title, subtitle = "尚無名單", "每日 18:00 官方籌碼定案後自動產出"
        return JSONResponse(json.loads(json.dumps({
            "date": used_date, "tier1": tier1, "tier2": [],
            "list_kind": kind, "title": title, "subtitle": subtitle,
            "today": _today, "next_trade_date": _next,
            "latest_fallback": is_latest_fallback, "note": note,
        }, default=str, ensure_ascii=False)))
    except Exception as e:
        return JSONResponse({"date": None, "tier1": [], "tier2": [],
                             "note": f"名單讀取失敗:{e}"})


@app.get("/api/market/official")
def api_market_official(date: str = None):
    """官方三大法人買賣超 + 大盤(給首頁 banner;有官方不自算)。"""
    try:
        import official_source as o
        d = datetime.strptime(date, "%Y-%m-%d") if date else None
        return JSONResponse(json.loads(json.dumps({
            "institutional": o.institutional_net(d),
            "index": o.market_index(d),
        }, default=str, ensure_ascii=False)))
    except Exception as e:
        return JSONResponse({"error": f"官方源讀取失敗:{e}"})


@app.get("/api/market/turnover-history")
def api_market_turnover_history(days: int = 10):
    """近 N 日大盤成交金額歷史(給「與前天比較」卡展開)。
    跳過假日/無資料的日,只回有成交金額的交易日。
    抓取上限 30 日(避免 API 被刷)。
    """
    try:
        import official_source as o
        from concurrent.futures import ThreadPoolExecutor
        from datetime import date as _date, timedelta
        n = max(1, min(int(days), 30))
        cache_key = n
        cached = getattr(api_market_turnover_history, "_cache", {}).get(cache_key)
        # 歷史成交量不是盤中即時資料；同一程序內快取 6 小時即可。
        if cached and time.time() - cached["at"] < 6 * 60 * 60:
            return JSONResponse(cached["payload"])

        today = _date.today()
        dates = [today - timedelta(days=k) for k in range(n + 5)]

        def fetch_day(d):
            try:
                idx = o.market_index(d)
                t = (idx or {}).get("turnover_100m")
                if t is not None:
                    return {"date": idx.get("date") or d.strftime("%Y%m%d"),
                            "turnover_100m": t}
            except Exception:
                pass
            return None

        # TWSE 每日查詢彼此獨立，並行抓取避免假日逐筆等待。
        with ThreadPoolExecutor(max_workers=8) as pool:
            fetched = list(pool.map(fetch_day, dates))
        out = [row for row in fetched if row is not None][:n]
        out.sort(key=lambda x: x["date"], reverse=True)
        payload = {"rows": out, "days": len(out), "note": None}
        cache = getattr(api_market_turnover_history, "_cache", {})
        cache[cache_key] = {"at": time.time(), "payload": payload}
        api_market_turnover_history._cache = cache
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({"rows": [], "days": 0, "error": f"成交金額歷史讀取失敗:{e}"})


@app.get("/api/market/index-history")
def api_market_index_history(start: str = "2026-07-22"):
    """加權指數歷史（固定資料），回傳起始日以來的收盤、點數與百分比。"""
    try:
        import official_source as o
        from concurrent.futures import ThreadPoolExecutor
        from datetime import date as _date, timedelta
        begin = _date.fromisoformat(start)
        end = _date.today()
        dates = []
        cursor = begin
        while cursor <= end:
            dates.append(cursor)
            cursor += timedelta(days=1)
        cache_key = begin.isoformat()
        cached = getattr(api_market_index_history, "_cache", {}).get(cache_key)
        if cached and cached["payload"].get("days", 0) > 1 and time.time() - cached["at"] < 6 * 60 * 60:
            return JSONResponse(cached["payload"])

        def fetch_day(d):
            try:
                idx = o.market_index(d) or {}
                if idx.get("taiex") is not None:
                    return {"date": idx.get("date") or d.strftime("%Y%m%d"),
                            "index": idx.get("taiex"),
                            "change": idx.get("change"),
                            "change_pct": idx.get("change_pct")}
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = [row for row in pool.map(fetch_day, dates) if row is not None]
        if len(rows) < 2:
            seed = [
                {"date":"20260804","index":43360.66,"change":-25.75,"change_pct":-0.06},
                {"date":"20260803","index":43386.41,"change":266.66,"change_pct":0.62},
                {"date":"20260731","index":43119.75,"change":3186.45,"change_pct":7.98},
                {"date":"20260730","index":39933.30,"change":-105.88,"change_pct":-0.26},
                {"date":"20260729","index":40039.18,"change":-1564.18,"change_pct":-3.76},
                {"date":"20260728","index":41603.36,"change":-2030.83,"change_pct":-4.65},
                {"date":"20260727","index":43634.19,"change":-20.65,"change_pct":-0.05},
                {"date":"20260724","index":43654.84,"change":-1195.97,"change_pct":-2.67},
                {"date":"20260723","index":44850.81,"change":25.03,"change_pct":0.06},
                {"date":"20260722","index":44825.78,"change":592.91,"change_pct":1.34},
            ]
            rows = list({r["date"]: r for r in seed + rows}.values())
        rows = [row for row in rows if begin.strftime("%Y%m%d") <= row["date"] <= end.strftime("%Y%m%d")]
        rows.sort(key=lambda x: x["date"], reverse=True)
        payload = {"rows": rows, "start": begin.isoformat(), "days": len(rows)}
        cache = getattr(api_market_index_history, "_cache", {})
        cache[cache_key] = {"at": time.time(), "payload": payload}
        api_market_index_history._cache = cache
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({"rows": [], "days": 0, "error": f"加權指數歷史讀取失敗:{e}"})


@app.get("/api/market/institution-history")
def api_market_institution_history(start: str = "2026-07-22"):
    """三大法人買賣超歷史（固定官方資料），回傳起始日以來的交易日。"""
    try:
        import official_source as o
        from concurrent.futures import ThreadPoolExecutor
        from datetime import date as _date, timedelta
        begin = _date.fromisoformat(start)
        end = _date.today()
        dates = []
        cursor = begin
        while cursor <= end:
            dates.append(cursor)
            cursor += timedelta(days=1)
        cache_key = begin.isoformat()
        cached = getattr(api_market_institution_history, "_cache", {}).get(cache_key)
        if cached and cached["payload"].get("days", 0) > 1 and time.time() - cached["at"] < 6 * 60 * 60:
            return JSONResponse(cached["payload"])

        def fetch_day(d):
            try:
                inst = o.institutional_net(d) or {}
                if inst.get("total_100m") is not None:
                    return {"date": inst.get("date") or d.strftime("%Y%m%d"),
                            "foreign_100m": inst.get("foreign_100m"),
                            "trust_100m": inst.get("trust_100m"),
                            "dealer_100m": inst.get("dealer_100m"),
                            "total_100m": inst.get("total_100m")}
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = [row for row in pool.map(fetch_day, dates) if row is not None]
        if len(rows) < 2:
            seed = [
                {"date":"20260804","foreign_100m":-57.32,"trust_100m":271.65,"dealer_100m":-194.29,"total_100m":20.05},
                {"date":"20260803","foreign_100m":-191.91,"trust_100m":233.25,"dealer_100m":-206.53,"total_100m":-165.20},
                {"date":"20260731","foreign_100m":675.54,"trust_100m":360.20,"dealer_100m":-162.60,"total_100m":873.14},
                {"date":"20260730","foreign_100m":-483.12,"trust_100m":139.97,"dealer_100m":-152.27,"total_100m":-495.41},
                {"date":"20260729","foreign_100m":-222.52,"trust_100m":57.06,"dealer_100m":-185.99,"total_100m":-351.45},
                {"date":"20260728","foreign_100m":-874.85,"trust_100m":16.12,"dealer_100m":-317.30,"total_100m":-1176.03},
                {"date":"20260727","foreign_100m":80.40,"trust_100m":4.81,"dealer_100m":-78.57,"total_100m":6.65},
                {"date":"20260724","foreign_100m":-609.50,"trust_100m":47.63,"dealer_100m":-111.51,"total_100m":-673.38},
                {"date":"20260723","foreign_100m":69.58,"trust_100m":73.70,"dealer_100m":41.94,"total_100m":185.22},
                {"date":"20260722","foreign_100m":173.44,"trust_100m":189.03,"dealer_100m":-23.98,"total_100m":338.49},
            ]
            rows = list({r["date"]: r for r in seed + rows}.values())
        rows = [row for row in rows if begin.strftime("%Y%m%d") <= row["date"] <= end.strftime("%Y%m%d")]
        rows.sort(key=lambda x: x["date"], reverse=True)
        payload = {"rows": rows, "start": begin.isoformat(), "days": len(rows)}
        cache = getattr(api_market_institution_history, "_cache", {})
        cache[cache_key] = {"at": time.time(), "payload": payload}
        api_market_institution_history._cache = cache
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({"rows": [], "days": 0, "error": f"法人歷史讀取失敗:{e}"})


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
                _fallback = _eod_state_latest()
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
def api_review(trade_date: str = ""):
    """盤後驗證頁：某交易日的名單驗證彙總 + 逐檔 T+1 判定 + 逐日趨勢。
    trade_date 省略時預設「最近一個有驗證資料的交易日」（非 today，避免
    週末/隔日開啟全空白）。"""
    # 保底:優先落在「真的有收盤資料」的驗證日,避免 B 卡顯示空白/尚未抓到資料。
    day = (trade_date or db.latest_review_date_with_data()
           or db.latest_review_date() or db.today())
    # 訊號日/驗證日:day＝結果蓋章日(＝驗證日);訊號日＝前一個有紀錄的交易日
    # (名單於前一交易日晚間產出)。取 dates 中 day 的下一筆(較舊)當訊號日。
    _dates = db.review_dates(90)
    signal_day = None
    if day in _dates:
        _i = _dates.index(day)
        signal_day = _dates[_i + 1] if _i + 1 < len(_dates) else None
    return JSONResponse({
        "trade_date": day,
        "verify_date": day,
        "signal_date": signal_day,
        "dates": db.review_dates(90),
        "recent_hit_rates": db.recent_hit_rates(30),
        "summary": db.review_summary(day),
        "outcomes": db.review_outcomes(day),
        "today": db.today_stats(),
        "watchlist_today": db.load_watchlist(day),
    })


@app.post("/api/admin/backfill-signal")
def api_backfill_signal(days: int = 20):
    """一次性回填歷史名單/驗證的『昨日訊號型態＋觸發原因』。跑在服務行程內、
    共用既有 Shioaji 連線(不另開行程,避免同金鑰重登踢掉行情 session)。
    只 UPDATE 新欄位,不動 verdict/命中率/報酬;跑前自動備份兩張表。"""
    import signal_backfill
    return JSONResponse(signal_backfill.run(days=days))


@app.get("/api/review/dates")
def api_review_dates():
    """有驗證資料的交易日清單（給日期選擇器）。"""
    return JSONResponse({"dates": db.review_dates(90)})


@app.get("/api/review/rejects")
def api_review_rejects(trade_date: str = ""):
    """某交易日落選池（含卡在哪個因子 / 七因子總分），供落選複盤。
    歷史日照實顯示（8/5 以前只有 radar 落選留痕，不可濾成空白）；
    「只留真淘汰」改由寫入端負責（after_hours 不再落地 radar_rejects），
    未來新產生的名單自然只剩 resilient/名額不足，歷史仍保留原樣可複盤。"""
    day = trade_date or db.latest_review_date() or db.today()
    rows = db.load_watch_rejects(day)
    # 說明語意層:每列補上白話 explain/tags/tier(唯一嘴巴,前端不再現算)
    for r in rows:
        r.update({k: v for k, v in explain.explain_row(r, kind="reject").items()
                  if k in ("explain", "tags", "tier", "tier_label")})
    # 今日盤中說明:淘汰理由 × 今日盤中資金流/漲跌 → 背離(誤刪候選)/確認(淘汰對了)。
    # 只在複盤日=今天時掛,拿今日盤中即時值;歷史日不臆造(否則拿今天流量套舊淘汰=誤導)。
    # aflow 走行程內韌性層:UNAVAILABLE 時 flow=None,intraday_note 自動標「資料未到」不偽裝。
    if day == db.today():
        try:
            live_map = _live_rows_map()
            for r in rows:
                code = str(r.get("stock_id") or r.get("code") or "")
                lv = live_map.get(code) or {}
                chg = lv.get("change_rate")
                flow = lv.get("aflow") if lv.get("aflow_status") != "UNAVAILABLE" else None
                why = r.get("detail") or r.get("reason") or r.get("fail_factor") or r.get("explain") or ""
                if lv.get("price") is not None:
                    r["today_price"] = lv.get("price")
                r["today_change"] = chg
                r["today_aflow"] = flow
                r["intraday_note"] = intraday_note.build(why, flow, chg)
        except Exception as _e:
            print(f"[review/rejects] intraday_note 跳過: {_e}", flush=True)
    return JSONResponse({"trade_date": day, "rows": rows})


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


@app.get("/api/dec/list")
def api_dec_list(date: str = ""):
    """統一名單驗證（新邏輯為主軸）：一份名單，一條生命線。
      dec_watchlist(某 target_date) × dec_verify(T+1蓋章) × 盤中 live(當日)。
      · target_date == 今天 → 掛即時價/漲跌/aflow，判定「盤中驗證中」（步驟2）。
      · target_date <  今天 → dec_verify 蓋章結果（真實命中 A / 報酬）（步驟4）。
    取代舊三套分歧（watch_outcome 空、legacy watchlist 停更），收斂到 dec_*。"""
    import statistics
    import decision_v22 as _D
    try:
        _D._init_tables()
    except Exception:
        pass
    with db._lock, db._conn() as c:
        if not date:
            # 預設落在「≤今天的最新 target_date」：今天→盤中live、過去→蓋章；
            # 不落在未來的明日名單(全待驗證、看不到東西)。都在未來才退回最新。
            r = c.execute(
                "SELECT MAX(target_date) d FROM dec_watchlist WHERE target_date<=?",
                (db.today(),)).fetchone()
            date = (r["d"] if r and r["d"] else None)
            if not date:
                r2 = c.execute("SELECT MAX(target_date) d FROM dec_watchlist").fetchone()
                date = (r2["d"] if r2 else None) or ""
        wl = [dict(x) for x in c.execute(
            """SELECT * FROM dec_watchlist WHERE target_date=?
               ORDER BY CASE grade WHEN 'Ready' THEN 0 ELSE 1 END, score DESC""",
            (date,))]
        vmap = {str(x["code"]): dict(x) for x in c.execute(
            "SELECT * FROM dec_verify WHERE target_date=?", (date,))}
        dates = [r["d"] for r in c.execute(
            "SELECT DISTINCT target_date d FROM dec_watchlist ORDER BY d DESC LIMIT 90")]
    is_today = bool(date) and (date == db.today())
    live = _live_rows_map() if is_today else {}
    # 法人連買天數改吃新鮮 chips_cache(source_date=最新交易日),不再讓前端從
    # dec_watchlist.reason 抽凍結的「連買N日」(decision_v22 舊殼理由會停在選股當下)。
    # 對齊個股卡片修法:同一事實只准一套算法,顯示端一律以 chips_cache 為準。
    _fresh_streak = {}
    try:
        _cc = json.loads(Path(__file__).with_name("chips_cache.json").read_text(encoding="utf-8"))
        for _cd, _r in (_cc.get("stocks") or {}).items():
            if _r.get("inst_streak") is not None:
                _fresh_streak[str(_cd)] = _r["inst_streak"]
    except Exception:
        pass
    rows, hits, ver_tot, rets = [], 0, 0, []
    to_persist = []   # write-through：is_today 時把 live aflow 落地到 dec_watchlist
    for w in wl:
        code = str(w["code"])
        v = vmap.get(code)
        lv = live.get(code) or {}
        verified = v is not None
        success = bool(v and v.get("success"))
        t1 = v.get("next_close_pct") if verified else None
        if verified:
            ver_tot += 1
            if success:
                hits += 1
            if t1 is not None:
                rets.append(t1)
            verdict = "命中" if success else "未命中"
            state = "verified"
        elif is_today:
            verdict, state = "盤中驗證中", "live"
        else:
            verdict, state = "待驗證", "pending"
        rows.append({
            "code": code, "name": w.get("name"), "sector": w.get("sector"),
            "track": w.get("track"), "grade": w.get("grade"), "score": w.get("score"),
            "reason": w.get("reason"), "signal_type": w.get("signal_type"),
            "entry_ref": w.get("trigger_price") or w.get("base_close"),
            "base_close": w.get("base_close"),
            "price": (lv.get("price") if is_today else None),
            # 盤中最高：僅今日盤中提供，供前端「今日觸發」三態燈判定曾觸及後回落
            "intraday_high": (lv.get("high") if is_today else None),
            "change_rate": (lv.get("change_rate") if is_today else t1),
            # 盤中 aflow：今日優先 live，live 沒有(收盤後快照過期)退回已存檔值；歷史日讀存檔值。不再一收盤就消失。
            "aflow": (lv.get("aflow") if (is_today and lv.get("aflow") is not None) else w.get("aflow")),
            "t1_close_pct": t1,
            "triggered": (v.get("triggered") if verified else None),
            "success": (success if verified else None),
            "hold_ret_pct": (v.get("hold_ret_pct") if verified else None),
            "verdict": verdict, "state": state, "verified": verified,
            "chip_label": ((lambda s: f"法人連{'買' if s > 0 else '賣'}{abs(int(s))}日")(_fresh_streak[code])
                           if _fresh_streak.get(code) else None),
        })
        if is_today and lv.get("aflow") is not None:
            to_persist.append((lv.get("aflow"), date, code))
    # write-through：把今日盤中 aflow 落地，收盤後即時快照過期也存得住（盤中資料收盤要存檔）
    if to_persist:
        try:
            with db._lock, db._conn() as c:
                for af, tdate, cd in to_persist:
                    c.execute("UPDATE dec_watchlist SET aflow=? WHERE target_date=? AND code=?",
                              (af, tdate, cd))
        except Exception as _e:
            print(f"[dec/list] aflow 存檔失敗:{_e}")
    median_ret = round(statistics.median(rets), 2) if rets else None
    return JSONResponse(json.loads(json.dumps({
        "target_date": date, "is_today": is_today, "rows": rows, "dates": dates,
        "total": len(wl), "verified_total": ver_tot,
        "hit_rate": (round(hits / ver_tot * 100, 1) if ver_tot else None),
        "median_return": median_ret,
        "note": ("target 是今天→盤中即時跟資金跑；是過去日→T+1 收盤蓋章結果。"
                 "真實命中＝dec_verify.success（Radar A 級 / 達標）。"),
    }, default=str, ensure_ascii=False)))


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


# ══════════════════════════════════════════════════════════
@app.get("/")
def home():
    html = (Path(__file__).resolve().parent.parent / "intraday_decision_dataflow.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


# /api/stock/{code} /api/report /api/watchpool — 給 NEXORA 決策頁內
# 側邊欄的「個股卡片」「每日報告」「觀察池 51 檔」分頁呼叫。
# 沒獨立的 /card /report /watchpool HTML 路由 — 全部走 /intraday-test。
# ══════════════════════════════════════════════════════════
import extras as _extras  # noqa: E402


def _read_html(filename: str) -> str:
    """中文檔名安全的 HTML 讀取 — Path.with_name 在某些 locale 會壞。
    先找 server.py 同層，找不到再回退 repo 根目錄（第一層 UI 等檔在根目錄）。"""
    here = _os.path.dirname(_os.path.abspath(__file__))
    for base in (here, _os.path.dirname(here)):
        p = _os.path.join(base, filename)
        if _os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
    raise FileNotFoundError(f"{filename} 不在 {here} 或其上層目錄")


@app.get("/watch-first-layer")
def watch_first_layer():
    """固定 51 檔觀察池第一層 UI；點擊個股再進完整卡片。"""
    return HTMLResponse(_read_html("個股第一層ＵＩ.html"), headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/stock/{code}")
def api_stock(code: str):
    """單檔個股決策卡 — 接 stock_card.build_card() + VPS Shioaji snap。"""
    try:
        return JSONResponse(_extras.build_stock_card(code))
    except Exception as exc:
        return JSONResponse({"ok": False, "code": code, "error": str(exc)}, status_code=500)


@app.get("/api/report")
def api_report():
    """每日 / 昨日盤後報告資料。"""
    try:
        return JSONResponse(_extras.build_report())
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/api/watchpool")
def api_watchpool():
    """51 檔觀察池全集 — 從 VPS Shioaji 訂閱 buffer 抓即時報價。"""
    try:
        return JSONResponse(_extras.build_watchpool())
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/api/card_page")
def api_card_page(code: str = "2337"):
    """個股卡片 HTML — 給 NEXORA 決策頁的 openStock popup iframe 用。
    直接回傳正式個股決策 UI，避免舊 redirect 指向不存在的檔案。"""
    try:
        p = _INTRADAY_ROOT / "5483_中美晶_個股決策UI.html"
        with open(p, "r", encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    except Exception as exc:
        return HTMLResponse(f"<h1>個股卡片載入失敗:{exc}</h1>", status_code=500)


@app.get("/api/watch-history")
def api_watch_history(date: str = None, days: int = 30):
    """盯盤名單歷史與準確度：每日名單的收盤結果 + 命中率，用來回頭優化篩選。"""
    try:
        rows = db.load_watch_outcome(date, days)
        by_day = {}
        for r in rows:
            by_day.setdefault(r["trade_date"], []).append(r)
        daily = []
        for d in sorted(by_day, reverse=True):
            items = by_day[d]
            hit = sum(1 for x in items if x.get("verdict") == "兌現")
            rev = sum(1 for x in items if x.get("verdict") == "反向")
            chs = [x["change_rate"] for x in items
                   if isinstance(x.get("change_rate"), (int, float))]
            daily.append({
                "trade_date": d, "total": len(items), "hit": hit, "reverse": rev,
                "hit_rate": round(hit / len(items) * 100, 1) if items else None,
                "avg_change": round(sum(chs) / len(chs), 2) if chs else None,
                "items": items,
            })
        allc = [x["change_rate"] for x in rows
                if isinstance(x.get("change_rate"), (int, float))]
        total, hits = len(rows), sum(1 for x in rows if x.get("verdict") == "兌現")
        return JSONResponse(json.loads(json.dumps({
            "ok": True, "days": len(daily), "daily": daily,
            "overall": {
                "total": total, "hit": hits,
                "hit_rate": round(hits / total * 100, 1) if total else None,
                "avg_change": round(sum(allc) / len(allc), 2) if allc else None,
            },
            "note": ("盯盤名單由前一晚 18:00 籌碼定案後產出；收盤 13:30 蓋章實際結果。"
                     "兌現=盤中升級可操作或漲幅≥1.5%且資金流入；反向=觸發風險或跌幅≥1.5%。"),
        }, default=str, ensure_ascii=False)))
    except Exception as e:
        return JSONResponse({"ok": False, "daily": [], "error": str(e)})


@app.post("/api/watch-history/stamp")
def api_watch_stamp(date: str = None):
    """手動補蓋章（收盤後若排程漏跑可呼叫）。"""
    try:
        n = stamp_watch_outcome(date or db.today())
        return JSONResponse({"ok": True, "stamped": n})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ══════════════════════════════════════════════════════════
# AB 引擎(8002 localhost-only)反向代理 — 盤後驗證分頁兩鏈資料出口
#   A 鏈(盤中觀測A)：/ab/watchlist + /ab/verify-stats
#   B 鏈(盤後驗證B)：/ab/b-discovery + /ab/pool-tomorrow
# 8002 只綁 127.0.0.1;由 8000 server 端轉,前端零跨域。
# 唯讀 GET、短逾時;AB 掛掉回 502 讓前端顯示「引擎未就緒」,不拖垮首頁。
# ══════════════════════════════════════════════════════════
import urllib.request as _urlreq
import urllib.parse as _urlparse
import urllib.error as _urlerr

_AB_BASE = "http://127.0.0.1:8002"


def _ab_get(path, params=None, timeout=20):
    # 前端主表的單次讀取上限是 8 秒；反向代理必須更早失敗，
    # 讓前端依約退回 /api/dec/list，而不是和瀏覽器同時卡到逾時。
    url = _AB_BASE + path
    if params:
        q = _urlparse.urlencode({k: v for k, v in params.items() if v is not None})
        if q:
            url += "?" + q
    try:
        with _urlreq.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            return JSONResponse(data, status_code=getattr(r, "status", 200))
    except _urlerr.HTTPError as e:
        return JSONResponse({"ok": False, "error": f"AB {e.code}", "detail": str(e.reason)}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": "AB 引擎未就緒", "detail": str(e)}, status_code=502)


@app.get("/ab/dropped")
def ab_dropped(pool_date: str = None, applies_date: str = None):
    """複盤:某池日被篩掉名單+今日盤中說明。
    applies_date=主表檢視日 → 引擎回該日『前一交易日』的池(=驗證組,看12→11、看10→7);
    pool_date=直接指定資料日(優先於 applies_date);皆空=預設今天的前一交易日。"""
    return _ab_get("/api/dropped", {"pool_date": pool_date, "applies_date": applies_date})


@app.get("/ab/watchlist")
def ab_watchlist(phase: str = None):
    """A 鏈名單(盤中觀測A)：phase 驅動 — 盤中=screen_intraday 嚴判燈號,盤後=screen_post 產池。"""
    return _ab_get("/api/watchlist", {"phase": phase}, timeout=5)  # 主表關鍵路徑:冷啟 fail-fast 退 dec/list


@app.get("/ab/verify-stats")
def ab_verify_stats(days: int = 30):
    """A 鏈 Learning 準確度：滾動勝率/報酬(screen_verify)。只顯示準度,不回改當日訊號。"""
    return _ab_get("/api/verify/stats", {"days": days})


@app.get("/ab/b-discovery")
def ab_b_discovery():
    """B 鏈今日盤中發現(唯讀)：非進場訊號,待盤後法人驗證。"""
    return _ab_get("/api/b/discovery")


@app.get("/ab/pool-tomorrow")
def ab_pool_tomorrow():
    """明日盤中候選池：merge_pool 匯流 A鏈寬篩 ∪ B鏈驗證通過。"""
    return _ab_get("/api/pool/tomorrow")


@app.get("/ab/verify-history")
def ab_verify_history(date: str = ""):
    """歷史回測：某日候選池「當時入選理由 vs T+1 最終結果」逐檔對照(screen_verify)。"""
    return _ab_get("/api/verify/history", {"date": date}, timeout=15)


@app.get("/ab/phase")
def ab_phase():
    """AB 引擎當下時段(PRE/INTRADAY/POST/CLOSED)。"""
    return _ab_get("/api/phase")


if __name__ == "__main__":
    _launch_scheduler()
    uvicorn.run(app, host="0.0.0.0", port=8000)
