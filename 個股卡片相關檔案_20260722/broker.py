"""
MLS 標準版 — broker.py
永豐 Shioaji 連線層。即時數據唯一來源。
金鑰一律從環境變數讀取,程式碼內不含任何金鑰:
    SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY
只用行情功能,不下單、不啟用 CA 憑證。
"""

import os
import time
import threading
from datetime import datetime
import shioaji as sj

def _diag(label, **fields):
    """低成本診斷記錄：不碰行情資料，只記時間、階段與耗時欄位。"""
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[diag][broker] {stamp} {label} {extra}", flush=True)

_api = None
_last_login = 0
RELOGIN_SEC = 20 * 3600   # Shioaji 需每24h重登,提前於20h重連

# ── 額度守門:當日 in-memory 快取(2026-07-14 修爆額度) ───────────────
# 盤中同一個 build_state() 內,kbars 跟 index_snapshot 被重複打,
# 一輪 50 檔 kbars + 3 次 index = 53 次,一天 50 輪 ≈ 2650 次,
# 直接打爆 Shioaji 連線額度 → Contract not found。
#
# 規則(簡單且夠用,過度設計才會壞事):
#   · kbars(code, days)        → 當日跨輪共用一份,key=code+days
#   · index_snapshot()         → 5s TTL(短到一輪 3 次呼叫全命中,長到
#                                 盤中每輪重抓仍算新鮮)
#   · 跨日(收盤 → 次日開盤)   → 全清,絕不把昨日 close 當今日用
#   · 取得失敗時快取負號結果 5s,避免「Shioaji 抖一下 → 整輪 50 個 kbars
#                                 全打失敗的失敗風暴」
_KBAR_CACHE = {}        # (code, days) → {"ts": epoch, "data": [...]}
_INDEX_CACHE = {}       # {"ts": epoch, "data": {...}, "err": bool}
_CACHE_DATE = ""        # 記當前快取歸屬的交易日;跨日清空
_CACHE_LOCK = threading.Lock()  # 避免盤中多執行緒同搶同一個 kbar

# ── [subscribe 改造] 行情推播緩衝區(不計流量,取代盤中 snapshots 輪詢) ──
# code → 最新報價 dict;由 _on_quote callback 持續更新。取價時從這裡拿,不打 API。
_QUOTE_BUF = {}
_QUOTE_LOCK = threading.Lock()
_SUBSCRIBED = set()             # 已送出訂閱的 code,避免重複訂閱
_QUOTE_CB_BOUND = False         # callback 是否已綁定(只綁一次)
_LOGIN_LOCK = threading.Lock()  # 防止多個 HTTP request 同時 login
_SUBSCRIBE_LOCK = threading.Lock()  # 防止多個 HTTP request 同時 subscribe


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _bust_cache_if_new_day():
    """跨日自動失效快取(收盤 → 次日開盤 不會把昨日收盤當今日用)。"""
    global _CACHE_DATE
    today = _today_str()
    if _CACHE_DATE != today:
        _KBAR_CACHE.clear()
        _INDEX_CACHE.clear()
        _CACHE_DATE = today


# ── 額度保險閥(2026-07-16 加) ─────────────────────────────────────
# 任何新 process 一登入就要 ~715MB baseline(login + fetch_contract)。
# 開新 process/容器 = 新 session = 額度直接 +715MB。
# 超過 400MB 拒絕 login,改用「已登入單例」或 raise,強制上層走 cache。
QUOTA_LIMIT_MB = 400
def _check_quota_or_refuse():
    """查目前額度(用 server.py 已登入 instance 的 api.usage(),不開新 process)。
    超過 QUOTA_LIMIT_MB 就 raise,讓 get_api() 走 reject 路徑。"""
    global _api
    if _api is None:
        return  # 還沒登入,允許第一次(必要的 baseline)
    try:
        usage = _api.usage()
        # usage 可能是 {bytes: int} 或物件;先試 bytes
        used = None
        if isinstance(usage, dict):
            used = usage.get("bytes") or usage.get("quota")
        else:
            used = getattr(usage, "bytes", None) or getattr(usage, "quota", None)
        if used is None:
            return  # 取不到就放行(不擋正常路徑)
        used_mb = used / (1024 * 1024)
        if used_mb > QUOTA_LIMIT_MB:
            raise RuntimeError(
                f"[broker] ⚠ 額度保險閥:已用 {used_mb:.0f}MB 超過 {QUOTA_LIMIT_MB}MB 門檻,"
                f"拒絕新 login(避免爆額度)。請等隔日額度重置或人工介入。")
    except RuntimeError:
        raise
    except Exception:
        return  # 查詢失敗不擋(避免 quota 查詢壞掉 → 整個系統癱瘓)


def get_api():
    """取得已登入的 Shioaji 實例;超過20小時自動重登。
    [保險閥] 額度超過 QUOTA_LIMIT_MB 拒絕 login,逼上層用 cache 或報錯。"""
    global _api, _last_login
    _diag("get_api.begin", has_api=_api is not None)
    if _api is not None and (time.time() - _last_login) < RELOGIN_SEC:
        _diag("get_api.reuse")
        return _api
    with _LOGIN_LOCK:
        # double-check：等待前一個 request 登入完成後直接共用同一 instance
        if _api is not None and (time.time() - _last_login) < RELOGIN_SEC:
            _diag("get_api.reuse_after_lock")
            return _api
        _check_quota_or_refuse()  # 只在「要重登」時檢查
        if _api is not None:
            try:
                _api.logout()
            except Exception:
                pass
        login_started = time.time(); _diag("shioaji.login.begin")
        _api = sj.Shioaji(simulation=False)   # 測試時可改 True
        try:
            _api.login(
                api_key=os.environ["SHIOAJI_API_KEY"],
                secret_key=os.environ["SHIOAJI_SECRET_KEY"],
                fetch_contract=True,
            )
        except TypeError:
            _api.login(
                api_key=os.environ["SHIOAJI_API_KEY"],
                secret_key=os.environ["SHIOAJI_SECRET_KEY"],
            )
        _last_login = time.time()
        _diag("shioaji.login.end", elapsed_ms=round((time.time()-login_started)*1000, 1))
        print("[broker] Shioaji 登入成功")
        return _api


def _bind_quote_callback():
    """綁定行情 callback(只綁一次)。tick 與 bidask 都收,更新緩衝區。"""
    global _QUOTE_CB_BOUND
    if _QUOTE_CB_BOUND:
        return
    started = time.time(); _diag("bind_callback.begin")
    api = get_api()

    @api.on_tick_stk_v1()
    def _on_quote(q):
        """Receive the tick stream used by subscribe(quote_type='tick')."""
        try:
            code = q.code
            # 用 getattr 容錯:不同 quote_type 欄位略有差異,缺的給 0/None
            close = float(getattr(q, "close", 0) or 0)
            with _QUOTE_LOCK:
                prev = _QUOTE_BUF.get(code, {})
                _QUOTE_BUF[code] = {
                    "code": code,
                    "price": close or prev.get("price", 0),
                    "open": float(getattr(q, "open", 0) or prev.get("open", 0) or 0),
                    "high": float(getattr(q, "high", 0) or prev.get("high", 0) or 0),
                    "low": float(getattr(q, "low", 0) or prev.get("low", 0) or 0),
                    # Shioaji 1.7 TickSTKv1 uses pct_chg/price-side fields.
                    "change_rate": float(getattr(q, "pct_chg", 0) or 0),
                    "volume_ratio": float(getattr(q, "volume_ratio", 0) or 0),
                    "total_volume": int(getattr(q, "total_volume", 0) or prev.get("total_volume", 0) or 0),
                    "total_amount": float(getattr(q, "total_amount", 0) or prev.get("total_amount", 0) or 0),
                    "avg_price": (float(getattr(q, "avg_price", 0) or 0)
                                  or prev.get("avg_price")),
                    "tick_type": getattr(q, "tick_type", None),
                    # 對下游統一語意：buy_volume=主動買(ask)，sell_volume=主動賣(bid)。
                    "buy_volume": int(getattr(q, "ask_side_total_vol", 0) or prev.get("buy_volume", 0) or 0),
                    "sell_volume": int(getattr(q, "bid_side_total_vol", 0) or prev.get("sell_volume", 0) or 0),
                }
        except Exception as e:
            print(f"[broker] quote callback 解析失敗 {getattr(q,'code','?')}: {e}")

    # Shioaji 不同版本對 decorator / quote 物件的 callback 掛載位置不同；
    # 只用 @api.on_tick_stk_v1() 時，部分 VPS 版本只收到 bidask、成交 tick 不進 buffer。
    # 再用目前 SDK 支援的 direct setter 綁一次，仍共用同一個 _on_quote，避免資料斷線。
    for target in (getattr(api, "quote", None), api):
        setter = getattr(target, "set_on_tick_stk_v1_callback", None) if target else None
        if setter:
            try:
                setter(_on_quote)
                print(f"[broker] tick callback direct bound on {type(target).__name__}")
            except Exception as e:
                print(f"[broker] tick direct callback 綁定失敗:{e}")

    event_setter = getattr(getattr(api, "quote", None), "set_on_event_callback", None)
    if event_setter:
        def _on_event(resp_code, event_code, info, event):
            _diag("shioaji.event", resp_code=resp_code, event_code=event_code, info=info, event=event)
            if event_code == 13:
                _diag("shioaji.reconnected")
        try:
            event_setter(_on_event)
            _diag("event_callback.bound")
        except Exception as e:
            _diag("event_callback.bind_failed", error=repr(e))

    _QUOTE_CB_BOUND = True
    _diag("bind_callback.end", elapsed_ms=round((time.time()-started)*1000, 1))
    print("[broker] 行情 callback 已綁定")


def ensure_subscribed(codes=None):
    """對固定池訂閱行情(只訂一次)。codes 預設為 config.UNIVERSE 那 51 檔。
    訂閱推播不計流量;絕不訂閱全市場。"""
    started = time.time(); _diag("subscribe.begin", requested=len(codes or []))
    with _SUBSCRIBE_LOCK:
        import config as C
        _bind_quote_callback()
        api = get_api()
        target = list(codes) if codes else list(getattr(C, "UNIVERSE", []))
        # 安全上限:防手滑訂到全市場
        if len(target) > getattr(C, "MAX_SUBSCRIBE", 180):
            print(f"[broker] ⚠ 訂閱數 {len(target)} 超過上限,截斷保護")
            target = target[:getattr(C, "MAX_SUBSCRIBE", 180)]
        for code in target:
            if code in _SUBSCRIBED:
                continue
            try:
                api.subscribe(api.Contracts.Stocks[code], quote_type="tick")
                api.subscribe(api.Contracts.Stocks[code], quote_type="bidask")
                _SUBSCRIBED.add(code)
            except Exception as e:
                print(f"[broker] 訂閱 {code} 失敗: {e}")
    _diag("subscribe.end", subscribed=len(_SUBSCRIBED), elapsed_ms=round((time.time()-started)*1000, 1))
    print(f"[broker] 已訂閱 {len(_SUBSCRIBED)} 檔(不計流量)")


def buffer_snapshots(codes):
    """[取代盤中 snapshots] 從行情緩衝區組出報價,欄位與 batch_snapshots 完全一致。
    尚未收到推播的 code 會缺席(跟 snapshots 空回行為一致,上層已能處理)。"""
    ensure_subscribed(codes)      # 確保已訂閱(冪等,只會實際訂一次)
    with _QUOTE_LOCK:
        out = [dict(_QUOTE_BUF[c]) for c in codes if c in _QUOTE_BUF]
        return out


def raw_buffer_snapshots():
    """Return every latest tick in the callback buffer, without code/filter gating."""
    started = time.time(); _diag("buffer.read.begin", size=len(_QUOTE_BUF))
    ensure_subscribed()
    with _QUOTE_LOCK:
        out = [dict(v) for v in _QUOTE_BUF.values()]
    _diag("buffer.read.end", size=len(out), elapsed_ms=round((time.time()-started)*1000, 1))
    return out


def market_scan_codes():
    """
    三排行榜聯集 → 全市場活躍股代碼(不佔訂閱額度)。
    """
    from config import SCANNER_TOP_N
    api = get_api()
    codes = set()
    for st in (
        sj.constant.ScannerType.ChangePercentRank,
        sj.constant.ScannerType.AmountRank,
        sj.constant.ScannerType.VolumeRank,
    ):
        try:
            for r in api.scanners(scanner_type=st, count=SCANNER_TOP_N):
                codes.add(r.code)
        except Exception as e:
            print(f"[broker] scanner {st} 失敗: {e}")
    return list(codes)


def batch_snapshots(codes, retries=3):
    """
    批次快照(不佔訂閱額度)。回傳 list[dict] 統一欄位:
    code, price, open, high, low, change_rate(%), volume_ratio,
    total_volume(股), total_amount(元), avg_price, tick_type

    開盤初期(09:00-09:30)流動性低、Shioaji 訂閱/合約載入未穩,
    整批空回是常態。加 retries:整批空 → 睡 2 秒 → 再試,最多 3 次。
    額度只多 2 次(共 3 次失敗才放棄),大幅降低「0 stocks」白屏。
    """
    api = get_api()
    contracts = []
    for c in codes:
        try:
            contracts.append(api.Contracts.Stocks[c])
        except Exception:
            continue
    if not contracts:
        return []

    for attempt in range(1, retries + 1):
        out = []
        for i in range(0, len(contracts), 400):      # snapshots 分批,控節奏防超限
            try:
                snaps = api.snapshots(contracts[i:i + 400])
            except Exception as e:
                print(f"[broker] snapshots 批次失敗(第 {attempt}/{retries} 次): {e}")
                continue
            for s in snaps:
                out.append({
                    "code": s.code,
                    "price": s.close,
                    "open": s.open, "high": s.high, "low": s.low,
                    "change_rate": s.change_rate,
                    "volume_ratio": getattr(s, "volume_ratio", 0) or 0,
                    "total_volume": (s.total_volume or 0),      # 股
                    "total_amount": (s.total_amount or 0),      # 元
                    "avg_price": getattr(s, "average_price", None),
                    "tick_type": getattr(s, "tick_type", None),
                    # Shioaji Snapshot 的 buy_volume 是 bid、sell_volume 是 ask；
                    # 對下游仍輸出 active_buy/active_sell 語意，故此處交換。
                    "buy_volume": getattr(s, "sell_volume", 0) or 0,
                    "sell_volume": getattr(s, "buy_volume", 0) or 0,
                })
            time.sleep(0.3)
        if out:
            if attempt > 1:
                print(f"[broker] snapshots 第 {attempt} 次重試拿到 {len(out)} 檔")
            return out
        # 整批空 → 等 2 秒重試
        if attempt < retries:
            time.sleep(2)
    print(f"[broker] snapshots {retries} 次全部空回,放棄")
    return out  # 空 list,呼叫端 fallback


def index_snapshot():
    """加權指數快照(Shioaji 指數合約 TSE001)。
    5s TTL 記憶體快取:一輪 build_state 內多次呼叫會全部命中同一份,
    避免盤中 50 輪 × 3 次/輪 = 150 次重複打 Shioaji。"""
    _bust_cache_if_new_day()
    with _CACHE_LOCK:
        now = time.time()
        if _INDEX_CACHE and (now - _INDEX_CACHE.get("ts", 0)) < 5:
            return _INDEX_CACHE.get("data") if not _INDEX_CACHE.get("err") else {}
    api = get_api()
    try:
        # shioaji 1.5+ 改 API:Indexs 是 ContractCategory,用 .get("TSE001") 取代舊的 .TSE["001"]
        contract = api.Contracts.Indexs.get("TSE001")
        snaps = api.snapshots([contract])
        if not snaps:
            raise RuntimeError("index snapshots 回空 list")
        s = snaps[0]
        data = {
            "index": s.close,
            "index_pct": round(s.change_rate, 2),
            "amount_100m": round((s.total_amount or 0) / 1e8, 0),
        }
        with _CACHE_LOCK:
            _INDEX_CACHE.clear()
            _INDEX_CACHE.update({"ts": time.time(), "data": data, "err": False})
        return data
    except Exception as e:
        print(f"[broker] 指數快照失敗: {e}")
        # 失敗時保留舊 cache 30 秒(避免 30 秒內 50 輪 × 1 次輪詢 = 1500 次重試失敗)
        # 也不要清空 sectors/STATE 連動 — 這層只動自己的 _INDEX_CACHE
        with _CACHE_LOCK:
            _INDEX_CACHE.clear()
            _INDEX_CACHE.update({"ts": time.time(), "data": {}, "err": True})
        return {}


def daily_kbars(code, days=70):
    """
    日K(供 MA20 / 60日前高 計算)。回傳 list[dict(date, close, high)]。
    當日 in-memory 快取:50 檔 × 50 輪 = 2500 次,改成 50 次真打 + 2450 次命中。
    跨日自動失效,絕不把昨日收盤當今日用。"""
    _bust_cache_if_new_day()
    key = (code, days)
    with _CACHE_LOCK:
        hit = _KBAR_CACHE.get(key)
        if hit is not None:
            return hit["data"]
    import pandas as pd
    from datetime import datetime, timedelta
    api = get_api()
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    try:
        # 支援股票代號 + 指數代號 (TSE001 等)
        # shioaji 1.5+ 改用 .get(code),舊的 [code] 失敗
        try:
            contract = api.Contracts.Stocks[code]
        except (KeyError, Exception):
            contract = api.Contracts.Indexs.get(code)
        kb = api.kbars(contract, start=start, end=end)
        df = pd.DataFrame({**kb})
        df["ts"] = pd.to_datetime(df["ts"])
        g = df.groupby(df["ts"].dt.date)
        daily = pd.DataFrame({
            "close": g["Close"].last(),
            "high": g["High"].max(),
            "low": g["Low"].min(),      # v2.4.1:補真實低點(六點50%回測/KD/ATR 轉精確)
            "open": g["Open"].first(),
            "volume": g["Volume"].sum(),
        }).tail(days)
        out = daily.reset_index().to_dict("records")
        with _CACHE_LOCK:
            _KBAR_CACHE[key] = {"ts": time.time(), "data": out}
        return out
    except Exception as e:
        print(f"[broker] kbars {code} 失敗: {e}")
        # 負快取 5s:Shioaji 抖一下時避免整輪 50 個 kbars 重打風暴
        with _CACHE_LOCK:
            _KBAR_CACHE[key] = {"ts": time.time(), "data": []}
        return []
