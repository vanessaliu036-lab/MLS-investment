"""
screen_verify.py — 當日收盤復盤(命中率 / 勝率分析層)

這是整條管線的最後一腿:
    盤後寬篩(screen_post) → 盤中嚴判(screen_intraday) → 當日收盤復盤(本支)

問的問題不同:
  b_verify   問「盤中發現的檔,今天法人有沒有買」(產生名單用)。
  screen_verify 問「昨天候選池那批,經過今天收盤,實際會不會賺」(衡量模型準度用)。
  兩者完全不同,不要混。

判定(對齊 screen_post 預標的進場軌,判定日 = T+1 收盤):
  攻擊軌(trigger=昨高)  觸發 = T+1 高 ≥ 觸發價;命中 = 觸發且 T+1 收 ≥ 觸發價(突破站穩)
  引擎軌(trigger=月線)  觸發 = T+1 收 ≥ 月線;命中 = 站月線且 T+1 收 ≥ 進場基準(收紅)
  觀察軌               不主動進場 → 只記錄、不計入命中率分母

門檻集中在檔頭常數,要調靈敏度只改這裡。命中率 = 命中數 / (攻擊+引擎 且有資料)。

2026-08-19 加規則歸因欄(tier / chase_risk / verification_status):
  不只算「入選那批對不對」,還要記「當時是被哪條規則降級/標記」,才能查
  「confidence<50 平均 T+1 還是漲」這種第三次反指標的根因,而不是猜門檻改多少。
  跟 reject_verify(結構失效淘汰那批)合起來,才是完整的「規則 → T+1」歸因表。

owner 規範:本支自建 pool_outcome 表,只寫這張;讀 candidate_pool / daily_bar。
與 A/B 兩鏈的名單產生完全脫鉤,這支爆掉不影響任何名單。
"""

from __future__ import annotations

import datetime as _dt
import json

import store
from envelope import run_all, persist_status, missing_labels
from phase import Phase, prev_trading_day, today_tw

PLUGIN = "screen_verify"
TABLE = "pool_outcome"

# ── 大盤 regime 排除(對齊 screen_intraday 盤中閘)─────────────────
# 命中率暴起暴落主因是 T+1 大盤方向(只做多動能策略的 beta)。盤中 regime 閘在下殺日
# 已把攻擊軌降續盯、不會進場 → 這種日子的攻擊軌也不該計入命中率分母(否則普跌日灌壞命中率)。
# 判定用 T+1 當天候選池上漲占比(base_close→next_close),可回填歷史、與盤中閘同門檻。
try:
    from screen_intraday import RISK_OFF_BREADTH_PCT
except Exception:
    RISK_OFF_BREADTH_PCT = 30.0

# ── 命中門檻(可調) ───────────────────────────────────────────────
ATTACK_HOLD = 1.0     # 攻擊軌:T+1 收盤 ≥ 觸發價 × 此倍數才算站穩(1.0=不跌破觸發價)
ENGINE_MIN_RET = 0.0  # 引擎軌:T+1 收盤相對進場基準的最低報酬%(0=收平即可)

_DDL = """
CREATE TABLE IF NOT EXISTS pool_outcome (
    data_date TEXT NOT NULL,      -- 判定日(T+1 收盤)
    pool_date TEXT,               -- 候選池產出日(T)
    code TEXT NOT NULL,
    track TEXT,
    trigger_price REAL,
    base_close REAL,              -- T 收盤(進場基準)
    next_high REAL,               -- T+1 高
    next_close REAL,              -- T+1 收
    ma20 REAL,
    triggered INTEGER,            -- 是否觸發進場
    hit INTEGER,                  -- 是否命中(觀察軌為 NULL,不計)
    ret_pct REAL,                 -- (T+1收 - 進場基準)/進場基準
    verdict TEXT,                 -- 命中 / 觸發未站穩 / 未觸發 / 觀察(不計)
    tier TEXT,                    -- layered_score 分層(🔥A級啟動/🔄反轉候選/⏳強勢但不追/👀保留觀察)
    chase_risk REAL,              -- T 日追價風險分數(禁追判準,診斷用,不影響 hit)
    verification_status TEXT,     -- B 鏈收盤驗證狀態(CONFIRMED/PARTIAL/UNCONFIRMED/NO_DATA/None=非B鏈)
    entry_status TEXT,            -- 禁止追高 / 等待觸發
    verified_at TEXT,
    PRIMARY KEY (data_date, code)
);
"""

# ── Phase 1 量測欄(attribution-only,不影響 hit / verdict)──────────────
# hit 的語意完全不變:仍是「依當初預標的進場軌,這筆交易做得成不成」。
# 三分命中率是並列新增,不是取代 —— 只刪分母會讓命中率變好看卻學不到東西。
_MEASURE_COLS_REAL = ("stock_ret_t1", "market_ret_t1", "sector_ret_t1",
                      "pool_median_ret_t1")
_MEASURE_COLS_INT = ("hit_abs", "hit_vs_market", "hit_vs_sector", "sector_peer_n_t1")
_MEASURE_COLS_TEXT = (
    "market_ret_t1_source", "sector_ret_t1_source",
    "market_regime", "market_regime_raw", "market_regime_source", "market_regime_version",
    "sector_regime", "sector_regime_source", "sector_regime_version",
    "sector_name",
)


_CODE_GROUP = None


def _code_group() -> dict:
    """族群對照 {code: 族群}。與 screen_post 同一份 config.CODE_GROUP;
    撞名(mls-v4 也有 config)時退回本檔同層 config.py 直讀。"""
    global _CODE_GROUP
    if _CODE_GROUP is not None:
        return _CODE_GROUP
    try:
        import config as _cfg
        _CODE_GROUP = getattr(_cfg, "CODE_GROUP", None) or {}
    except Exception:
        _CODE_GROUP = {}
    if not _CODE_GROUP:
        try:
            import importlib.util as _ilu
            from pathlib import Path as _P
            _spec = _ilu.spec_from_file_location(
                "_verify_config", _P(__file__).resolve().parent / "config.py")
            _m = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_m)
            _CODE_GROUP = getattr(_m, "CODE_GROUP", None) or {}
        except Exception:
            _CODE_GROUP = {}
    return _CODE_GROUP


def _median(vals):
    """中位數。缺值不參與,全缺回 None(不猜、不用 0 頂替)。"""
    import statistics
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 2) if vals else None


# ── 真實大盤基準(TAIEX)──────────────────────────────────────────────
# ⚠ 為什麼非接不可:原本拿「51 檔池的報酬中位數」當大盤,但那 51 檔全是半導體/電子,
#   實測與真實 TAIEX 平均差 2.76pp、最大 12.81pp ——
#     2026-08-04  池中位 +12.75%  vs  真實 TAIEX -0.06%   (差 12.81pp,方向相反)
#     2026-08-03  池中位  +9.81%  vs  真實 TAIEX +0.62%   (差 9.19pp)
#   拿這種基準算「贏大盤」,結論會整個反過來。
# 資料源 TWSE FMTQIK(每日市場成交資訊,含 TAIEX 收盤與漲跌點),官方免費、約一個月滾動。
_INDEX_URL = "https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK"
_INDEX_CACHE = {"ts": 0.0, "data": None}
_INDEX_TTL = 1800


def _roc_to_iso(s):
    s = str(s or "").strip()
    if len(s) >= 7 and s.isdigit():
        return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
    return None


def fetch_index_returns(force: bool = False) -> dict:
    """{ISO 日期: 當日 TAIEX 漲跌%}。取數失敗回 {} —— 不猜、不用池中位頂替。"""
    import time
    import urllib.request
    now = time.time()
    if not force and _INDEX_CACHE["data"] is not None and (now - _INDEX_CACHE["ts"]) < _INDEX_TTL:
        return _INDEX_CACHE["data"]
    out = {}
    try:
        req = urllib.request.Request(_INDEX_URL, headers={"User-Agent": "MLS/4.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read().decode("utf-8"))
        for row in rows:
            d = _roc_to_iso(row.get("Date"))
            try:
                close = float(str(row.get("TAIEX", "")).replace(",", ""))
                chg = float(str(row.get("Change", "")).replace(",", ""))
            except (TypeError, ValueError):
                continue
            prev = close - chg
            if d and prev:
                out[d] = round(chg / prev * 100, 2)
        _INDEX_CACHE.update({"ts": now, "data": out})
    except Exception as e:
        print(f"[screen_verify] TAIEX 取數失敗(市場基準留空,不用池中位頂替): "
              f"{type(e).__name__}: {e}", flush=True)
    return out


def _rate(items: list[dict], key: str, scored_only: bool = True):
    """某個三分命中欄的命中率(%)。None 不計入分母。

    ⚠ scored_only 預設 True:母體必須是「可判定(攻擊/引擎軌)」那批,不能用全 51。
      用全池母體時「贏大盤」在池中位基準下照定義就是約 50%(實測每天都 49.0%),
      毫無資訊量 —— 有意義的問法是「被選進場的那批,有沒有贏過基準」。
    """
    src = [r for r in items if r.get("hit") is not None] if scored_only else items
    vals = [r.get(key) for r in src if r.get(key) is not None]
    return round(sum(vals) / len(vals) * 100, 1) if vals else None


def _pct_below_t1(bar: dict, key: str):
    """T+1 當日池內收盤跌破指定均線的比例(%)。缺值不計入分母。"""
    total = hit = 0
    for _row in bar.values():
        close, ma = (_row or {}).get("close"), (_row or {}).get(key)
        if close is None or not ma:
            continue
        total += 1
        hit += (close < ma)
    return round(hit / total * 100, 1) if total else None


def _ensure_table(db_path: str = "mls.db") -> None:
    with store.conn(db_path) as c:
        c.executescript(_DDL)
        # 舊表補欄(2026-08-19 新增規則歸因欄位)
        cols = {r[1] for r in c.execute("PRAGMA table_info(pool_outcome)").fetchall()}
        for col in ("tier", "verification_status", "entry_status"):
            if col not in cols:
                c.execute(f"ALTER TABLE pool_outcome ADD COLUMN {col} TEXT")
        if "chase_risk" not in cols:
            c.execute("ALTER TABLE pool_outcome ADD COLUMN chase_risk REAL")
        # Phase 1 量測欄補欄(同一套 ALTER 相容模式,舊列留 NULL 不回填假值)。
        # 逐欄各自 try:本函式在 module import 時就會跑,一欄失敗若往上冒
        # 會讓整個 screen_verify import 不進去 → 連既有命中率都一起沒了。
        # 量測欄補不上最壞只是那欄留空,不值得賠掉整支模組。
        for cols_group, sqltype in ((_MEASURE_COLS_REAL, "REAL"),
                                    (_MEASURE_COLS_INT, "INTEGER"),
                                    (_MEASURE_COLS_TEXT, "TEXT")):
            for col in cols_group:
                if col in cols:
                    continue
                try:
                    c.execute(f"ALTER TABLE pool_outcome ADD COLUMN {col} {sqltype}")
                except Exception as e:
                    print(f"[screen_verify] 量測欄 {col} 補欄失敗(略過): {e}", flush=True)
        c.commit()
    try:
        store.register_table(TABLE, PLUGIN)
    except store.TableOwnershipError:
        pass  # 已註冊


_ensure_table()


def judge_row(track: str, base_close, trigger_price, next_high, next_close, ma20):
    """純函式:單檔判定。回傳 (triggered, hit, ret_pct, verdict)。可單測。"""
    ret = (round((next_close - base_close) / base_close * 100, 2)
           if (base_close and next_close is not None) else None)

    if track == "觀察":
        return None, None, ret, "觀察(不計)"

    if track == "攻擊軌":
        if next_high is None or trigger_price is None:
            return None, None, ret, "資料不足"
        triggered = 1 if next_high >= trigger_price else 0
        if not triggered:
            return 0, 0, ret, "未觸發"
        hit = 1 if (next_close is not None and next_close >= trigger_price * ATTACK_HOLD) else 0
        return 1, hit, ret, ("命中" if hit else "觸發未站穩")

    if track == "引擎軌":
        if next_close is None or ma20 is None or base_close is None:
            return None, None, ret, "資料不足"
        triggered = 1 if next_close >= ma20 else 0
        if not triggered:
            return 0, 0, ret, "跌破月線"
        hit = 1 if (ret is not None and ret >= ENGINE_MIN_RET) else 0
        return 1, hit, ret, ("命中" if hit else "站月線未收紅")

    return None, None, ret, "未知軌別"


def verify(db_path: str = "mls.db", data_date: _dt.date | None = None) -> dict:
    """
    用 data_date(T+1)當天收盤,復盤 pool_date(前一交易日)產出的候選池。
    """
    _ensure_table(db_path)
    d = data_date or today_tw()
    pool_date = prev_trading_day(d)

    envs = run_all({
        "pool": lambda: store.read_date("candidate_pool", pool_date, db_path),
        "bar": lambda: store.read_date("daily_bar", d, db_path),
        # T 日(pool_date)全 51 收盤:算 T+1 全市場寬度用(對齊盤中 regime 閘的母體,非只看選中的池成員)
        "bar_prev": lambda: store.read_date("daily_bar", pool_date, db_path),
    }, phase=Phase.POST)
    persist_status(envs, db_path)

    pool = envs["pool"].get({}) or {}
    bar = envs["bar"].get({}) or {}
    bar_prev = envs["bar_prev"].get({}) or {}

    if not pool:
        return {
            "phase": "POST", "data_date": d.isoformat(), "pool_date": pool_date.isoformat(),
            "purpose": f"當日收盤復盤 — {pool_date} 無候選池可驗",
            "degraded": missing_labels(envs), "items": [],
            "denom": 0, "hits": 0, "hit_rate": None,
        }

    now = _dt.datetime.now().isoformat(timespec="seconds")
    rows, items = [], []
    for code, prow in pool.items():
        payload = {}
        if prow.get("payload"):
            try:
                payload = json.loads(prow["payload"])
            except Exception:
                pass
        base_close = payload.get("close")
        track = prow.get("track")
        trig = prow.get("trigger_price")
        b = bar.get(code) or {}
        nh, nc, ma20 = b.get("high"), b.get("close"), b.get("ma20")

        triggered, hit, ret, verdict = judge_row(track, base_close, trig, nh, nc, ma20)
        rec = {
            "data_date": d.isoformat(), "pool_date": pool_date.isoformat(), "code": code,
            "track": track, "trigger_price": trig, "base_close": base_close,
            "next_high": nh, "next_close": nc, "ma20": ma20,
            "triggered": triggered, "hit": hit, "ret_pct": ret,
            "verdict": verdict,
            "tier": payload.get("tier"),
            "chase_risk": payload.get("chase_risk"),
            "verification_status": payload.get("verification_status"),
            "entry_status": payload.get("entry_status"),
            "verified_at": now,
        }
        rows.append(rec)
        items.append(rec)

    # ── T+1 大盤 regime 排除:普跌日攻擊軌不計命中(對齊盤中 regime 閘)──────────
    # 寬度用全 51 universe(daily_bar T+1 vs T 收盤),不是只看選中的池成員 —— 與盤中閘同母體,
    # 否則「我的選股跌得比大盤兇」會誤判 Risk Off、排除盤中閘根本沒擋的日子。上漲占比 <門檻 = Risk Off。
    ups = tot = 0
    for _c, _bb in bar.items():
        _nc = (_bb or {}).get("close")
        _pc = (bar_prev.get(_c) or {}).get("close")
        if _nc is not None and _pc:
            tot += 1
            ups += (_nc > _pc)
    breadth_pct = round(ups / tot * 100, 1) if tot else None
    risk_off = breadth_pct is not None and breadth_pct < RISK_OFF_BREADTH_PCT
    regime_excluded_codes = set()
    if risk_off:
        for r in items:
            # 攻擊軌(追突破)當天會被盤中 regime 閘擋下、不進場 → hit 設 None 剔出分母
            if r["track"] == "攻擊軌" and r["hit"] is not None:
                r["hit"] = None
                r["verdict"] = f"大盤RiskOff({breadth_pct}%)·攻擊軌不追(不計)"
                regime_excluded_codes.add(r["code"])
    regime_excluded = len(regime_excluded_codes)

    # ── Phase 1 三分命中率(絕對 / 相對大盤 / 相對族群)────────────────────
    # 為什麼要三個:南亞科 -6.6% vs 大盤 -2% vs 族群 -4% = 三項全敗 = 真的挑錯;
    # 但 -0.5% vs 大盤 -2% vs 族群 -4% 在舊制同樣被打成 Fail,實際上它非常強。
    # 把後者當普通選股失敗會讓人往錯的方向改篩選器。
    #
    # ⚠ 整段包 try:量測層沒有資格弄垮 15:05 的當日復盤。掛掉最壞是新欄位留 None,
    #   絕不能變成「今天沒有 pool_outcome」——那會讓命中率頁整天空白。
    market_t1 = None
    try:
        _measure_three_way(items, pool, bar, bar_prev, d.isoformat())
    except Exception as _e:
        print(f"[screen_verify] Phase1 三分命中率降級(不影響 hit): {_e}", flush=True)
    else:
        market_t1 = next((r.get("market_ret_t1") for r in items
                          if r.get("market_ret_t1") is not None), None)

    store.upsert_intraday(TABLE, PLUGIN, rows, db_path)
    return _finish_verify(items, rows, pool, envs, d, pool_date, now,
                          breadth_pct, risk_off, regime_excluded_codes, bar, market_t1)


def _measure_three_way(items, pool, bar, bar_prev, _dstr) -> None:
    """把三分命中率與環境快照掛到每一列(就地改)。attribution-only。"""
    _cg = _code_group()
    t1_ret = {}
    for _c, _bb in bar.items():
        _nc = (_bb or {}).get("close")
        _pc = (bar_prev.get(_c) or {}).get("close")
        if _nc is not None and _pc:
            t1_ret[_c] = round((_nc - _pc) / _pc * 100, 2)
    # 大盤基準:真實 TAIEX 優先;取不到就留 None,不用池中位頂替
    # (池中位是「這 51 檔電子股」,不是大盤 —— 兩者實測平均差 2.76pp)。
    _idx = fetch_index_returns()
    market_t1 = _idx.get(_dstr)
    market_src = "taiex" if market_t1 is not None else None
    pool_median_t1 = _median(list(t1_ret.values()))   # 保留當診斷,名字誠實
    sector_members = {}
    for _c in t1_ret:
        sector_members.setdefault(_cg.get(_c), []).append(_c)

    for r in items:
        code = r["code"]
        payload = pool.get(code, {})
        try:
            pl = json.loads(payload["payload"]) if payload.get("payload") else {}
        except Exception:
            pl = {}
        sec = _cg.get(code)
        # peer-exclusive:族群基準要排除自己,否則小族群裡「贏過族群」變成部分在跟自己比
        peers = [x for x in sector_members.get(sec, []) if x != code]
        sector_t1 = _median([t1_ret[x] for x in peers])
        # 用 daily_bar 同一基準(T收→T+1收)算個股報酬,才跟大盤/族群可比。
        # 與既有 ret_pct 可能有微小差異:ret_pct 的基準是選股當下 payload 的 close,
        # 可能來自即時報價回填。ret_pct 語意不動,這裡另存一欄。
        sret = t1_ret.get(code)
        r.update({
            "stock_ret_t1": sret,
            "market_ret_t1": market_t1,
            "market_ret_t1_source": market_src,
            "pool_median_ret_t1": pool_median_t1,   # 診斷用,不是大盤
            "sector_ret_t1": sector_t1,
            "sector_ret_t1_source": "pool51_peer" if sector_t1 is not None else None,
            "sector_peer_n_t1": len(peers),
            "sector_name": sec,
            "hit_abs": (None if sret is None else int(sret > 0)),
            "hit_vs_market": (None if (sret is None or market_t1 is None)
                              else int(sret > market_t1)),
            "hit_vs_sector": (None if (sret is None or sector_t1 is None)
                              else int(sret > sector_t1)),
            # 選股當時的環境快照(由 candidate_pool.payload 帶過來,不重算)
            "market_regime": pl.get("market_regime"),
            "market_regime_raw": pl.get("market_regime_raw"),
            "market_regime_source": pl.get("market_regime_source"),
            "market_regime_version": pl.get("market_regime_version"),
            "sector_regime": pl.get("sector_regime"),
            "sector_regime_source": pl.get("sector_regime_source"),
            "sector_regime_version": pl.get("sector_regime_version"),
        })


# 來回交易成本(%)。手續費牌告 0.1425%×6折×雙邊 = 0.171%,加賣出證交稅 0.3%。
# 現股當沖證交稅減半(0.15%)→ 32.1 bps,但盤後名單是隔日進場,用一般稅率。
ROUND_TRIP_COST_PCT = 0.471


def _track_breakdown(items, regime_excluded_codes, market_t1) -> dict:
    """依軌道拆成:候選數 → 觸發率 → 觸發後勝率 → 觸發後淨報酬/Alpha。

    為什麼要拆:攻擊軌的觸發價是 max(昨高,前日高) 的結構壓力位,衝高收黑日會
    拉到 10%+ 變不可觸發。把「沒觸發」算成交易失敗,等於拿沒下的單算勝負 ——
    實測攻擊軌混合命中率 31.9%,但拆開後觸發後勝率 54.7%,跟引擎軌一樣好。
    未觸發從此只算「沒有形成交易」,不進勝率分母。
    """
    out = {}
    for track in ("攻擊軌", "引擎軌"):
        cand = [r for r in items if r.get("track") == track]
        if not cand:
            continue
        excluded = [r for r in cand if r["code"] in regime_excluded_codes]
        # regime 閘擋下的當天根本不進場,不算候選失敗也不算沒觸發
        evaluable = [r for r in cand if r["code"] not in regime_excluded_codes
                     and r.get("triggered") is not None]
        trig = [r for r in evaluable if r.get("triggered") == 1]
        scored = [r for r in trig if r.get("hit") is not None]
        rets = [r["ret_pct"] for r in trig if r.get("ret_pct") is not None]
        alphas = [r["ret_pct"] - market_t1 for r in trig
                  if r.get("ret_pct") is not None and market_t1 is not None]
        avg = (lambda xs: round(sum(xs) / len(xs), 2) if xs else None)
        out[track] = {
            "candidates": len(cand),
            "excluded_by_regime": len(excluded),
            "no_data": len([r for r in cand if r.get("triggered") is None
                            and r["code"] not in regime_excluded_codes]),
            "evaluable": len(evaluable),
            "triggered": len(trig),
            "no_trade": len(evaluable) - len(trig),      # 未觸發 = 沒有形成交易
            "trigger_rate": (round(len(trig) / len(evaluable) * 100, 1)
                             if evaluable else None),
            "win_rate_after_trigger": (
                round(sum(1 for r in scored if r["hit"] == 1) / len(scored) * 100, 1)
                if scored else None),
            "win_n_after_trigger": sum(1 for r in scored if r["hit"] == 1),
            "scored_after_trigger": len(scored),
            "avg_ret_after_trigger": avg(rets),
            "avg_net_ret_after_trigger": (
                round(sum(rets) / len(rets) - ROUND_TRIP_COST_PCT, 2) if rets else None),
            # Alpha 基準是 TAIEX(market_ret_t1),不是池中位 —— 池中位是這 51 檔
            # 電子股,拿它當大盤會讓 Alpha 變成自己跟自己比。
            "avg_alpha_vs_taiex_after_trigger": avg(alphas),
            "cost_pct_assumed": ROUND_TRIP_COST_PCT,
        }
    return out


def _finish_verify(items, rows, pool, envs, d, pool_date, now,
                   breadth_pct, risk_off, regime_excluded_codes, bar, market_t1) -> dict:
    """彙總回傳。hit / denom / hit_rate 的算法與語意完全不變。

    track_breakdown 是並列新增的分母拆解(#6),不改 hit_rate —— 舊欄位還在,
    要換算法是之後的決定,不在這一步偷偷做掉。"""
    regime_excluded = len(regime_excluded_codes)
    scored = [r for r in items if r["hit"] is not None]     # 攻擊+引擎(有資料;普跌日攻擊軌已剔除)
    denom = len(scored)
    hits = sum(1 for r in scored if r["hit"] == 1)
    rets = [r["ret_pct"] for r in scored if r["ret_pct"] is not None]
    rets_sorted = sorted(rets)
    median = (rets_sorted[len(rets_sorted) // 2] if rets_sorted else None)

    items.sort(key=lambda r: (0 if r["hit"] == 1 else 1 if r["hit"] == 0 else 2,
                              -(r["ret_pct"] or -999)))
    return {
        "phase": "POST", "data_date": d.isoformat(), "pool_date": pool_date.isoformat(),
        "purpose": (f"當日收盤復盤:候選池 {len(pool)} 檔,可判定 {denom} 檔,"
                    f"命中 {hits} → 命中率 {round(hits/denom*100,1) if denom else '—'}%"),
        "verified_at": now,
        "degraded": missing_labels(envs),
        "pool_size": len(pool), "denom": denom, "hits": hits,
        "hit_rate": round(hits / denom * 100, 1) if denom else None,
        "breadth_pct": breadth_pct, "risk_off": risk_off,
        "regime_excluded": regime_excluded,
        # #6 攻擊軌分母修正:候選數 → 觸發率 → 觸發後勝率 → 觸發後淨報酬/Alpha
        "track_breakdown": _track_breakdown(items, regime_excluded_codes, market_t1),
        # Phase 1 三分命中率:母體是「全部有 T+1 報酬的列」,不套進場軌過濾,
        # 也不做 regime 排除 —— 這三個數字的用途是診斷哪一層失效,
        # 不是拿來當戰績,所以刻意不縮分母。
        "market_context_t1": {
            "market_ret_t1": market_t1,
            "market_ret_t1_source": ("taiex" if market_t1 is not None else None),
            # 池中位另列,名字誠實 —— 它是「這 51 檔電子股」不是大盤,
            # 實測與 TAIEX 平均差 2.76pp、最大 12.81pp,不可混用。
            "pool_median_ret_t1": next(
                (r.get("pool_median_ret_t1") for r in items
                 if r.get("pool_median_ret_t1") is not None), None),
            "breadth_pct": breadth_pct,
            "below_ma5_pct": _pct_below_t1(bar, "ma5"),
            "below_ma20_pct": _pct_below_t1(bar, "ma20"),
        },
        "hit_abs_rate": _rate(items, "hit_abs"),
        "hit_vs_market_rate": _rate(items, "hit_vs_market"),
        "hit_vs_sector_rate": _rate(items, "hit_vs_sector"),
        "ret_median": median,
        "ret_avg": round(sum(rets) / len(rets), 2) if rets else None,
        "items": items,
    }


_RS_BUCKETS = (("Top 20%", 80), ("20–40%", 60), ("40–60%", 40),
               ("60–80%", 20), ("Bottom 20%", 0))

_ATTR_AGG = """COUNT(*) n,
               AVG(stock_ret_t1) avg_t1,
               SUM(CASE WHEN hit_abs=1 THEN 1 ELSE 0 END) hit_abs,
               SUM(CASE WHEN hit_abs IS NOT NULL THEN 1 ELSE 0 END) hit_abs_n,
               SUM(CASE WHEN hit_vs_market=1 THEN 1 ELSE 0 END) hit_vs_market,
               SUM(CASE WHEN hit_vs_market IS NOT NULL THEN 1 ELSE 0 END) hit_vs_market_n,
               SUM(CASE WHEN hit_vs_sector=1 THEN 1 ELSE 0 END) hit_vs_sector,
               SUM(CASE WHEN hit_vs_sector IS NOT NULL THEN 1 ELSE 0 END) hit_vs_sector_n"""


def _finish_attr(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["avg_t1"] = round(r["avg_t1"], 2) if r["avg_t1"] is not None else None
        for k in ("hit_abs", "hit_vs_market", "hit_vs_sector"):
            n = r.pop(f"{k}_n", 0) or 0
            r[f"{k}_rate"] = round(r[k] / n * 100, 1) if n else None
    return rows


def _attribution_tables(since: str, db_path: str) -> dict:
    """Phase 1 四張歸因表。

    刻意「不」一次 GROUP BY 市場×族群×RS×軌道五層:以目前樣本數會全部變成
    n=1、n=2,什麼都看不出來。先各自看一維,樣本夠大再 drill-down 交叉。

    母體用 stock_ret_t1 IS NOT NULL(全部有 T+1 報酬的列),不篩 hit ——
    要看的是每一層環境本身對 T+1 的方向,不是被進場軌過濾後的殘存樣本。
    """
    out = {}
    with store.conn(db_path) as c:
        out["by_market_regime"] = _finish_attr([dict(r) for r in c.execute(
            f"""SELECT market_regime, {_ATTR_AGG}
                FROM pool_outcome
                WHERE data_date >= ? AND stock_ret_t1 IS NOT NULL
                      AND market_regime IS NOT NULL
                GROUP BY market_regime""", (since,))])
        out["by_sector_regime"] = _finish_attr([dict(r) for r in c.execute(
            f"""SELECT sector_regime, {_ATTR_AGG}
                FROM pool_outcome
                WHERE data_date >= ? AND stock_ret_t1 IS NOT NULL
                      AND sector_regime IS NOT NULL
                GROUP BY sector_regime""", (since,))])
        out["by_track_market_regime"] = _finish_attr([dict(r) for r in c.execute(
            f"""SELECT track, market_regime, {_ATTR_AGG}
                FROM pool_outcome
                WHERE data_date >= ? AND stock_ret_t1 IS NOT NULL
                      AND market_regime IS NOT NULL AND track IS NOT NULL
                GROUP BY track, market_regime""", (since,))])

    # RS 分桶:percentile 存在 candidate_pool.payload(選股當日橫截面 freeze),
    # pool_outcome 沒有這欄 → 由 payload 讀回來對齊,不在這裡重算
    # (重算會變成用「驗證當天」的橫截面,問錯問題)。
    buckets: dict[str, list] = {label: [] for label, _ in _RS_BUCKETS}
    # candidate_pool 是別人(screen_post)的表,本支只讀不寫;新裝的庫/測試庫可能還沒有它。
    # 量測層絕不能因為讀不到別人的表就把 stats() 弄垮 —— 拿不到就這張表留空。
    try:
        with store.conn(db_path) as c:
            rows = [dict(r) for r in c.execute(
                """SELECT o.pool_date, o.code, o.stock_ret_t1, o.hit_abs,
                          o.hit_vs_market, o.hit_vs_sector, p.payload
                   FROM pool_outcome o JOIN candidate_pool p
                     ON p.data_date = o.pool_date AND p.code = o.code
                   WHERE o.data_date >= ? AND o.stock_ret_t1 IS NOT NULL""", (since,))]
    except Exception as e:
        rows = []
        out["by_relative_strength_error"] = f"{type(e).__name__}: {e}"[:120]
    for r in rows:
        try:
            pct = (json.loads(r["payload"]) or {}).get("market_rel_pctile")
        except Exception:
            pct = None
        if pct is None:
            continue
        for label, lo in _RS_BUCKETS:
            if pct >= lo:
                buckets[label].append(r)
                break

    by_rs = []
    for label, _lo in _RS_BUCKETS:
        grp = buckets[label]
        if not grp:
            continue
        rets = [g["stock_ret_t1"] for g in grp if g["stock_ret_t1"] is not None]
        row = {"bucket": label, "n": len(grp),
               "avg_t1": round(sum(rets) / len(rets), 2) if rets else None}
        for k in ("hit_abs", "hit_vs_market", "hit_vs_sector"):
            vals = [g[k] for g in grp if g[k] is not None]
            row[k] = sum(vals) if vals else 0
            row[f"{k}_rate"] = round(sum(vals) / len(vals) * 100, 1) if vals else None
        by_rs.append(row)
    out["by_relative_strength"] = by_rs
    out["attribution_note"] = (
        "四張一維表,不做五層交叉(樣本數不足會全變 n=1~2)。母體=有 T+1 報酬的全部列,"
        "不套進場軌過濾、不做 regime 排除 —— 用途是診斷哪一層失效,不是戰績。"
        "hit_vs_market/hit_vs_sector 才分得出「真的挑錯」與「絕對跌但相對強」。")
    return out


def stats(days: int = 30, db_path: str = "mls.db") -> dict:
    """滾動 N 個交易日的勝率/報酬(從 pool_outcome 彙總)。模型的真正驗證。"""
    _ensure_table(db_path)
    since = (today_tw() - _dt.timedelta(days=days)).isoformat()
    with store.conn(db_path) as c:
        by_track = [dict(r) for r in c.execute(
            """SELECT track,
                      COUNT(*) n,
                      SUM(CASE WHEN hit IS NOT NULL THEN 1 ELSE 0 END) scored,
                      SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) hits,
                      AVG(ret_pct) avg_ret
               FROM pool_outcome
               WHERE data_date >= ? AND hit IS NOT NULL
               GROUP BY track""", (since,))]
        daily = [dict(r) for r in c.execute(
            """SELECT data_date,
                      SUM(CASE WHEN hit IS NOT NULL THEN 1 ELSE 0 END) scored,
                      SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) hits
               FROM pool_outcome WHERE data_date >= ?
               GROUP BY data_date ORDER BY data_date""", (since,))]
    for t in by_track:
        t["hit_rate"] = round(t["hits"] / t["scored"] * 100, 1) if t["scored"] else None
        t["avg_ret"] = round(t["avg_ret"], 2) if t["avg_ret"] is not None else None
    for row in daily:
        row["hit_rate"] = round(row["hits"] / row["scored"] * 100, 1) if row["scored"] else None
    total_scored = sum(t["scored"] for t in by_track)
    total_hits = sum(t["hits"] for t in by_track)

    # 規則歸因(2026-08-19):不篩 hit,吃全部有 T+1 收盤的列——因為 tier/chase_risk/
    # verification_status 影響的是「該不該追」,不是「會不會漲」,要看的是原始 ret_pct,
    # 不能用 hit(已經被進場軌邏輯過濾過)當母體,否則看不出規則本身的方向對不對。
    with store.conn(db_path) as c:
        by_tier = [dict(r) for r in c.execute(
            """SELECT tier,
                      COUNT(*) n,
                      AVG(ret_pct) avg_ret,
                      SUM(CASE WHEN ret_pct >= 5 THEN 1 ELSE 0 END) up5,
                      SUM(CASE WHEN ret_pct < 0 THEN 1 ELSE 0 END) down
               FROM pool_outcome
               WHERE data_date >= ? AND ret_pct IS NOT NULL AND tier IS NOT NULL
               GROUP BY tier""", (since,))]
        by_verification = [dict(r) for r in c.execute(
            """SELECT verification_status,
                      COUNT(*) n,
                      AVG(ret_pct) avg_ret,
                      SUM(CASE WHEN ret_pct >= 5 THEN 1 ELSE 0 END) up5,
                      SUM(CASE WHEN ret_pct < 0 THEN 1 ELSE 0 END) down
               FROM pool_outcome
               WHERE data_date >= ? AND ret_pct IS NOT NULL AND verification_status IS NOT NULL
               GROUP BY verification_status""", (since,))]
    for t in (by_tier + by_verification):
        t["avg_ret"] = round(t["avg_ret"], 2) if t["avg_ret"] is not None else None
        t["up5_rate"] = round(t["up5"] / t["n"] * 100, 1) if t["n"] else None
        t["down_rate"] = round(t["down"] / t["n"] * 100, 1) if t["n"] else None
    by_tier.sort(key=lambda t: -(t["avg_ret"] if t["avg_ret"] is not None else -999))
    by_verification.sort(key=lambda t: -(t["avg_ret"] if t["avg_ret"] is not None else -999))

    # 量測層失敗不得拖垮命中率統計本身(owner 規範:這支爆掉不影響任何名單,
    # 同理歸因層爆掉也不該影響它在量測的數字)。
    try:
        attribution = _attribution_tables(since, db_path)
    except Exception as e:
        attribution = {"attribution_error": f"{type(e).__name__}: {e}"[:200]}

    return {
        "window_days": days, "since": since,
        "overall_hit_rate": round(total_hits / total_scored * 100, 1) if total_scored else None,
        "scored": total_scored, "hits": total_hits,
        "by_track": by_track, "daily": daily,
        "by_tier": by_tier, "by_verification": by_verification,
        **attribution,
        "note": ("by_tier/by_verification 是規則歸因(rule attribution):某 tier/驗證狀態的股票,"
                 "T+1 實際平均報酬多少。avg_ret 若對 ⏳強勢但不追 或 UNCONFIRMED 這種'降級但未淘汰'"
                 "的分類反而是正的高值,代表該條規則正在製造反指標,不是門檻要微調,是方向錯了。"),
    }
