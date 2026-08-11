"""
screen_post.py — 盤後寬篩(產出隔日盤中候選池)

定位:這份清單不是進場名單,是候選池。
      任務不是「選出會漲的」,是「篩掉明天盤中不值得盯的」。

為什麼要寬:
  錯放的成本很低 —— 隔天盤中還有一關嚴判會淘汰它。
  漏放的成本很高 —— 沒進候選池,你一整天不會再看它一眼。
  所以寧可多留幾檔,不要少留。

輸出:15-20 檔候選池,每檔預先標好隔日的進場軌與進場條件。
      判斷前移到盤後,盤中只執行、不臨場決策 —— 這是命中率的來源。

資料來源:今日法人蓋章值 + 今日融資 + 今日收盤量價。
      不使用 aflow(盤中推估值)。盤後已經有真的法人數字,不需要推估。

與 screen_intraday 完全不共用計分函式。這支改壞不影響那支。
"""

from __future__ import annotations

import datetime as _dt
import json

import store
import config
import layered_score
from envelope import run_all, persist_status, missing_labels
from phase import Phase, prev_trading_day, next_trading_day, today_tw

# collect.py 為重用 mls-v4 取數，把 mls-v4/app 插到 sys.path 最前，`config`
# 會被撞名成 mls-v4 那支（無 NAME）。screen_post 只靠 config 取 NAME，故此處
# 從「本檔同層的 config.py」直接讀 NAME，避免撞名 AttributeError。
_NAME_MAP = None


def _name_map():
    global _NAME_MAP
    if _NAME_MAP is not None:
        return _NAME_MAP
    _NAME_MAP = getattr(config, "NAME", None)
    if not _NAME_MAP:                      # 撞名成 mls-v4 → 從本地 config.py 讀
        _NAME_MAP = _local_config_attr("NAME") or {}
    return _NAME_MAP


_CODE_GROUP = None


def _code_group() -> dict:
    """族群對照 {code: 族群}。與 _name_map 同樣防 config 撞名。"""
    global _CODE_GROUP
    if _CODE_GROUP is not None:
        return _CODE_GROUP
    _CODE_GROUP = getattr(config, "CODE_GROUP", None)
    if not _CODE_GROUP:
        _CODE_GROUP = _local_config_attr("CODE_GROUP") or {}
    return _CODE_GROUP


def _local_config_attr(name: str):
    """撞名時直接從本檔同層 config.py 讀指定屬性。"""
    try:
        import importlib.util as _ilu
        from pathlib import Path as _P
        _p = _P(__file__).resolve().parent / "config.py"
        _spec = _ilu.spec_from_file_location("_screen_config", _p)
        _m = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_m)
        return getattr(_m, name, None)
    except Exception:
        return None


def _read_regime() -> dict:
    """Layer 0 真實寬度(TWSE 漲家數/總家數)。取數失敗回 unknown — 不假裝、不當 Risk On。"""
    try:
        import market_regime as _mr
        b = _mr.fetch_breadth()
        tb = b.get("true_breadth")
        if tb is None:
            return {"unknown": True, "risk_off": False}
        pct = round(tb * 100, 1)
        return {"breadth_pct": pct, "advancing": b.get("advancing"),
                "declining": b.get("declining"), "total": b.get("total"),
                "risk_off": pct < 30, "risk_on": pct >= 70, "unknown": False}
    except Exception as e:
        return {"unknown": True, "risk_off": False, "error": str(e)[:80]}


def _pool_purpose(applies, pool, regime) -> str:
    """誠實 purpose:講明適用日、市場狀態、資料是否就緒;不硬寫『只盯這些』的假結論。"""
    n = len(pool)
    scored = [it for it in pool if (it.get("score") or 0) > 0]
    if not scored:
        return f"適用 {applies}(次一交易日)— 今日資料尚未就緒,{n} 檔候選待收盤更新後才有效"
    if regime.get("risk_off"):
        return (f"適用 {applies}(次一交易日)· 市場 Risk Off"
                f"(真實寬度 {regime.get('breadth_pct')}%,漲 {regime.get('advancing')}/跌 {regime.get('declining')})"
                f"— 禁新倉,{n} 檔僅追蹤觀察,非進場名單")
    if regime.get("unknown"):
        return f"適用 {applies}(次一交易日)— {n} 檔候選,非進場名單(市場寬度取數失敗,待修)"
    return f"適用 {applies}(次一交易日)— {n} 檔候選,非進場名單"

PLUGIN = "screen_post"
TABLE = "watchlist_post"
POOL_TABLE = "candidate_pool"   # 隔日盤中只盯這張表
DROPPED_TABLE = "dropped_pool"  # 被淘汰名單留痕(真結構失效),供淘汰名單顯示/複盤

store.register_table(POOL_TABLE, PLUGIN)

POOL_SIZE = 20          # 候選池上限。寬,不是準。
DROP_THRESHOLD = 2      # 硬性排除:中兩個條件就砍(已依指示放寬)


# ============================================================ 第一層:硬性排除
#
# 只問一個問題:這檔明天盤中值得盯嗎?
# 三態鐵律:任一項 NO_DATA 一律不算數、不計入砍數。
# 資料沒接入就誤殺 = 你截圖那個「54% 卡在未達門檻」的病,絕不重演。

def hard_drop(bar: dict | None, inst: dict | None) -> tuple[bool, list[str]]:
    hits: list[str] = []

    # 條件1:爆量收黑且跌破月線 —— 趨勢已壞
    if bar:
        c, o, ma20 = bar.get("close"), bar.get("open"), bar.get("ma20")
        vol, vma = bar.get("volume"), bar.get("vol_ma20")
        if None not in (c, o, ma20, vol, vma) and vma:
            if vol > vma * 2.0 and c < o and c < ma20:
                hits.append("爆量收黑且破月線")

    # 條件2:法人連續賣超 3 日以上 —— 主力在跑
    if inst and inst.get("consecutive_days") is not None:
        if inst["consecutive_days"] <= -3:
            hits.append("法人連賣3日以上")

    # 條件3:量能低於均量 50% —— 沒人玩,盤中不會有戲
    if bar:
        vol, vma = bar.get("volume"), bar.get("vol_ma20")
        if vol is not None and vma:
            if vol < vma * 0.5:
                hits.append("量能不足均量五成")

    return len(hits) >= DROP_THRESHOLD, hits


# ============================================================ 第二層:候選池排序
#
# 權重為「隔天盤中會不會啟動」服務,不是為「今天表現好不好」。

W = {
    "money_health": 25,   # 資金健康度:核心主軸,錢有沒有停在這
    "inst_streak": 20,    # 法人連買:隔天有人續抬轎的機率
    "margin": 15,         # 融資減:籌碼乾淨,隔天不容易被獲利盤壓
    "vs_ma20": 15,        # 收在月線上:攻擊軌突破的前提位置
    "absorption": 15,     # 承接品質:昨天有人接,隔天下殺才有支撐
    "volume": 10,         # 溫和放量加分,爆量扣分
}


def _norm(x, lo, hi):
    if x is None:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) if hi > lo else 0.0


def score_one(code, bar, inst, margin, health, absorb) -> dict:
    pts = 0.0
    reasons: list[str] = []
    missing: list[str] = []

    if health and health.get("score") is not None:
        v = _norm(health["score"], 0, 100)
        pts += W["money_health"] * v
        if v > 0.6:
            reasons.append("資金健康度佳")
    else:
        missing.append("資金健康度")

    if inst and inst.get("consecutive_days") is not None:
        d = inst["consecutive_days"]
        if d > 0:
            pts += W["inst_streak"] * _norm(d, 0, 5)
            reasons.append(f"法人連買{d}日")
        elif d < 0:
            pts -= W["inst_streak"] * 0.3 * _norm(-d, 0, 5)
            reasons.append(f"法人連賣{-d}日")
    else:
        missing.append("法人連買天數")

    if margin and margin.get("margin_change") is not None:
        ch = margin["margin_change"]
        if ch < 0:
            pts += W["margin"]
            reasons.append("融資減(籌碼乾淨)")
        elif ch > 0:
            pts -= W["margin"] * 0.4
            reasons.append("融資增")
    else:
        missing.append("融資")

    close = (bar or {}).get("close")
    ma20 = (bar or {}).get("ma20")
    if close is not None and ma20:
        if close >= ma20:
            pts += W["vs_ma20"]
            reasons.append("收在月線上")
        else:
            reasons.append("收破月線")
    else:
        missing.append("MA20")   # 沒接入 = 不計分,絕不判死刑

    if absorb and absorb.get("score") is not None:
        v = _norm(absorb["score"], 0, 100)
        pts += W["absorption"] * v
        if v > 0.6:
            reasons.append("承接品質強")
    else:
        missing.append("承接品質")

    vol = (bar or {}).get("volume")
    vma = (bar or {}).get("vol_ma20")
    if vol is not None and vma:
        r = vol / vma
        if 1.2 <= r <= 2.0:
            pts += W["volume"]
            reasons.append("溫和放量")
        elif r > 3.0:
            pts -= W["volume"] * 0.5
            reasons.append("爆量")
    else:
        missing.append("量能")

    return {
        "code": code, "score": round(max(0.0, min(100.0, pts)), 1),
        "close": close, "reasons": reasons, "missing": missing,
        "has_data": bar is not None,
    }


# ============================================================ 第三層:標進場軌
#
# 每檔預先標好隔日的進場條件。
# 隔天盤中你看的不是「誰分數高」,是「哪幾檔到了我預設的進場條件」。
# 判斷前移到盤後,盤中只執行 —— 命中率的提升來自這裡。

# 升級閘門檻:盤中資金健康度、法人態度、可容忍的月線下方距離。
# 定案(2026-08-11):分軌原本只看價格(close≥ma20)+量,盤中資金與盤後籌碼沒進到
# 「可操作」這一關 → 選股日在月線下但盤中大買、隔日續強的贏家(如 2492/3026)被丟觀察軌
# 不計命中,月線上的弱勢股反而進攻擊/引擎軌。升級閘:資金強 AND 法人買時,close 在月線下
# STRONG_BELOW_MA20_PCT 內也可升『引擎軌』(等站回月線+法人買進場),兩關真正進到可操作閘。
STRONG_HEALTH_MIN = 60.0        # money_health score ≥ 此值 = 盤中資金健康度強(對齊 score_one「>0.6 佳」)
STRONG_BELOW_MA20_PCT = 3.0     # close 在月線下方、但差距 ≤ 此% → 升級閘可考慮


def _strong_money(health: dict | None) -> bool:
    s = (health or {}).get("score")
    return s is not None and s >= STRONG_HEALTH_MIN


def _inst_buying(inst: dict | None) -> bool:
    """法人買超/連買:當日總淨額為正,或法人連買(consecutive_days>0)。"""
    i = inst or {}
    cd, tn = i.get("consecutive_days"), i.get("total_net")
    return (cd is not None and cd > 0) or (tn is not None and tn > 0)


def assign_track(bar: dict | None, item: dict,
                 health: dict | None = None, inst: dict | None = None) -> dict:
    close = (bar or {}).get("close")
    high = (bar or {}).get("high")
    ma20 = (bar or {}).get("ma20")
    vol = (bar or {}).get("volume")
    vma = (bar or {}).get("vol_ma20")

    on_ma20 = close is not None and ma20 and close >= ma20
    volumed = vol is not None and vma and vol >= vma * 1.2
    # 月線下、但差距在容忍% 內(如 close ≥ ma20×0.97)
    near_below_ma20 = (close is not None and ma20 and close < ma20
                       and close >= ma20 * (1 - STRONG_BELOW_MA20_PCT / 100))

    if on_ma20 and volumed:
        item["track"] = "攻擊軌"
        item["entry_rule"] = f"等突破昨高 {high},ATR 停損"
        item["trigger_price"] = high
    elif on_ma20:
        item["track"] = "引擎軌"
        item["entry_rule"] = f"等回月線 {ma20} 支撐 + 法人買進,月線停損"
        item["trigger_price"] = ma20
    elif near_below_ma20 and _strong_money(health) and _inst_buying(inst):
        # 升級閘:月線下 ≤3% + 盤中資金強 + 法人買 → 引擎軌(需站回月線才進場,月線停損)
        item["track"] = "引擎軌"
        item["entry_rule"] = f"月線下但盤中資金強+法人買,等站回月線 {ma20} 確認進場,月線停損"
        item["trigger_price"] = ma20
        item["track_upgraded"] = True   # 標記升級來源,供量測升級閘是否真的接對贏家
    else:
        item["track"] = "觀察"
        item["entry_rule"] = "位置不佳,等盤中訊號,不主動進場"
        item["trigger_price"] = None
    return item


# ============================================================ 多日衍生(給 layered_score)
#
# 現有 DB 就有多日 daily_bar / inst_flow,直接算出對昨收漲跌、連漲天數、
# 近 3/5 日法人累計 —— 讓 layered_score 這幾項不再 Pending,盤後判斷更準。

def _derive_multiday(code: str, d: _dt.date, db_path: str) -> dict:
    bars = store.read_recent("daily_bar", code, d, 6, db_path)     # 新→舊,含 d 當日
    insts = store.read_recent("inst_flow", code, d, 5, db_path)
    change_rate = up_days = inst_3d = inst_5d = None

    closes = [x.get("close") for x in bars if x.get("close") is not None]
    if len(closes) >= 2 and closes[1]:
        change_rate = round((closes[0] - closes[1]) / closes[1] * 100, 2)
    if closes:
        ud = 0
        for today_c, prev_c in zip(closes, closes[1:]):
            if today_c is not None and prev_c is not None and today_c > prev_c:
                ud += 1
            else:
                break
        up_days = ud

    nets = [x.get("total_net") for x in insts if x.get("total_net") is not None]
    if len(nets) >= 3:
        inst_3d = sum(nets[:3])
    if len(nets) >= 5:
        inst_5d = sum(nets[:5])

    return {"change_rate": change_rate, "up_days": up_days,
            "inst_3d": inst_3d, "inst_5d": inst_5d}


def _relative_strength(universe: list[str], derivs: dict) -> dict:
    """族群強度 + 相對大盤(個股漲跌 − 基準漲跌)。

    族群基準 = 同族群成員漲跌中位數(config.CODE_GROUP);
    大盤基準 = 整池漲跌中位數(daily_bar 無加權指數,故以 51 池中位數代理;
              對「我盯的候選誰相對強」其實比對 TAIEX 更貼題)。缺 change_rate 者不參與、標 None(Pending)。
    """
    import statistics
    from collections import defaultdict
    cg = _code_group()
    chg = {c: derivs.get(c, {}).get("change_rate") for c in universe}
    valid = [v for v in chg.values() if v is not None]
    market = statistics.median(valid) if valid else None
    sec_vals: dict[str, list] = defaultdict(list)
    for c in universe:
        s, v = cg.get(c), chg[c]
        if s and v is not None:
            sec_vals[s].append(v)
    sec_med = {s: statistics.median(vs) for s, vs in sec_vals.items() if vs}
    out = {}
    for c in universe:
        v, s = chg[c], cg.get(c)
        sr = (round(v - sec_med[s], 2) if (v is not None and s in sec_med) else None)
        mr = (round(v - market, 2) if (v is not None and market is not None) else None)
        out[c] = {"sector_rel": sr, "market_rel": mr}
    return out


# ============================================================ 主流程

def build(universe: list[str], db_path: str = "mls.db",
          data_date: _dt.date | None = None) -> dict:
    d = data_date or today_tw()

    envs = run_all({
        "bar": lambda: store.read_date("daily_bar", d, db_path),
        "inst": lambda: store.read_date("inst_flow", d, db_path),
        "margin": lambda: store.read_date("margin", d, db_path),
        "health": lambda: store.read_date("money_health", d, db_path),
        "absorb": lambda: store.read_date("absorption", d, db_path),
    }, phase=Phase.POST)
    persist_status(envs, db_path)

    b = envs["bar"].get({}) or {}
    i = envs["inst"].get({}) or {}
    m = envs["margin"].get({}) or {}
    h = envs["health"].get({}) or {}
    ab = envs["absorb"].get({}) or {}

    # 多日衍生 + 族群/大盤相對強度(前置一次算好,迴圈裡引用)
    derivs = {c: _derive_multiday(c, d, db_path) for c in universe}
    rels = _relative_strength(universe, derivs)

    kept, dropped = [], []
    for c in universe:
        # 雙分數分層接管淘汰(治誤刪):只有 tier==淘汰(≥2結構失效)才移出主名單;
        # 強勢禁追/核心/候補全部保留。舊 hard_drop 仍算,但只當「量測對照」記錄,不再當閘。
        lay = layered_score.score_layered(
            layered_score.build_input(c, b.get(c), i.get(c),
                                      **derivs[c], **rels[c]))
        old_drop, old_hits = hard_drop(b.get(c), i.get(c))   # 舊制對照,供誤刪率量測
        lay_fields = {
            "tier": lay["tier"], "continuation": lay["continuation"],
            "chase_risk": lay["chase_risk"], "chase_safety": lay["chase_safety"],
            "chip_status": lay["chip_status"],
            "structural_failures": lay["structural_failures"],
            "old_would_drop": old_drop, "old_drop_hits": old_hits,
        }
        if lay["tier"] == layered_score.TIER_REJECTED:
            # 真淘汰:帶結構失效原因(不是「分數低」),供淘汰名單顯示與 T+1 錯殺量測
            dropped.append({"code": c, "why": lay["structural_failures"] or old_hits,
                            **lay_fields})
            continue
        it = score_one(c, b.get(c), i.get(c), m.get(c), h.get(c), ab.get(c))
        it = assign_track(b.get(c), it, health=h.get(c), inst=i.get(c))
        it.update(lay_fields)
        it["layered_reasons"] = lay["reasons"]
        it["layered_risks"] = lay["risks"]
        it["pending_factors"] = lay["pending"]
        kept.append(it)

    # 排序改吃「延續機率」(明天值不值得看),而非舊單一 score;同分以追價安全高者優先。
    # 保留 score 於 payload 供相容/對照,但不再主導名單順序。
    _tier_rank = {layered_score.TIER_CORE: 0, layered_score.TIER_NO_CHASE: 1,
                  layered_score.TIER_CANDIDATE: 2}
    kept.sort(key=lambda x: (_tier_rank.get(x["tier"], 3),
                             -(x.get("continuation") or 0),
                             -(x.get("chase_safety") or 0), x["code"]))
    pool = kept[:POOL_SIZE]
    for n, it in enumerate(pool, 1):
        it["rank"] = n

    # 名稱注入(事實對照,非 mock);缺名則留空,不編造
    name_map = _name_map()
    cg = _code_group()
    for it in kept:
        it["name"] = name_map.get(it["code"])
        it["sector"] = cg.get(it["code"])   # 族群注入(事實對照),前端所有 AB 表族群不再空白
        _streak = (i.get(it["code"]) or {}).get("consecutive_days")
        it["inst_streak"] = _streak          # 連買連賣 streak(顯示端並列用)
        it["chip_label"] = _chip_label(it.get("chip_status"), _streak)  # 含背離並顯的籌碼標籤
        it["explain"] = _item_explain(it)     # 觀察軌白話說明(語意層),取代 reasons 原文

    # Layer 0 閘(鐵律6):Risk Off → 禁新倉,全數降觀察、清進場軌與觸發價
    regime = _read_regime()
    if regime.get("risk_off"):
        for it in pool:
            it["track"] = "觀察"
            it["entry_rule"] = "市場 Risk Off·禁新倉,僅追蹤不進場"
            it["trigger_price"] = None

    gen = _dt.datetime.now().isoformat(timespec="seconds")

    # 寫前清該日舊列(治同日重建殘留:落選碼舊列會留著顯示缺籌碼/錯 chip)
    _purge_date(POOL_TABLE, d, db_path)
    _purge_date(DROPPED_TABLE, d, db_path)
    # 候選池落地 —— 隔天盤中只讀這張表
    store.upsert_intraday(POOL_TABLE, PLUGIN, [{
        "data_date": d.isoformat(), "code": it["code"],
        "rank": it["rank"], "score": it["score"],
        "track": it["track"],
        "trigger_price": it.get("trigger_price"),
        "entry_rule": it["entry_rule"],
        "payload": json.dumps(it, ensure_ascii=False),
        "generated_at": gen,
    } for it in pool], db_path)

    store.upsert_intraday(TABLE, PLUGIN, [{
        "data_date": d.isoformat(), "code": it["code"],
        "rank": it.get("rank", 999), "score": it["score"],
        "payload": json.dumps(it, ensure_ascii=False), "generated_at": gen,
    } for it in kept], db_path)

    # 淘汰名單留痕(真結構失效):存 dropped_pool,供顯示端「被篩掉名單」與 T+1 複盤,不刪除。
    if dropped:
        store.upsert_intraday(DROPPED_TABLE, PLUGIN, [{
            "data_date": d.isoformat(), "code": it["code"],
            "payload": json.dumps(it, ensure_ascii=False), "generated_at": gen,
        } for it in dropped], db_path)

    try:
        store.snapshot_post(d, db_path)
    except Exception:
        pass

    tracks: dict[str, int] = {}
    for it in pool:
        tracks[it["track"]] = tracks.get(it["track"], 0) + 1

    applies = next_trading_day(d).isoformat()
    return {
        "phase": "POST", "data_date": d.isoformat(), "applies_date": applies,
        "regime": regime,
        "purpose": _pool_purpose(applies, pool, regime),
        "actionable": False,
        "generated_at": gen,
        "degraded": missing_labels(envs),
        "universe_size": len(universe),
        "dropped_count": len(dropped),
        "pool_size": len(pool),
        "track_breakdown": tracks,
        "items": pool,
        "dropped": [_shape_dropped_row(x["code"], x, _name_map(), _code_group())
                    for x in dropped],
    }


def load_pool(data_date: _dt.date | None = None, db_path: str = "mls.db") -> dict[str, dict]:
    """隔天盤中呼叫這支拿候選池。預設讀上一個交易日產出的那份。"""
    d = data_date or prev_trading_day()
    return store.read_date(POOL_TABLE, d, db_path)


def _shape_dropped_row(code, p, name_map, cg):
    """把一列淘汰(build 的原始 dropped 或 dropped_pool payload)映成顯示端欄位。
    build() 與 load_dropped() 共用這支,兩條路形狀一致(根治名字/族群空白)。"""
    sf = p.get("structural_failures") or p.get("why") or []
    detail = "、".join(sf) if isinstance(sf, list) else str(sf)
    return {
        "stock_id": code, "stock_name": name_map.get(code, code),
        "sector": cg.get(code), "source": "ab",
        "fail_factor": "結構失效", "detail": detail,
        "tier": p.get("tier"), "tier_label": "淘汰",
        "explain": (f"{detail} → 結構失效,移出主名單。" if detail
                    else "結構失效(2 項以上),移出主名單。"),
        "tags": [],
        "continuation": p.get("continuation"), "chase_risk": p.get("chase_risk"),
    }


_TIER_ACTION = {
    "核心觀察": "結構最強,列核心續盯。",
    "強勢觀察｜禁止追價": "禁開盤追,等回測昨高或 5MA。",
    "候補觀察": "候補,盤中觸發再升級。",
}


def _chip_label(chip_status, streak):
    """盤後法人籌碼顯示字串(語意層,不參與篩選):含連買/連賣 streak;
    chip_status(單日)與 streak 背離(今日買但連賣)時並顯,消除『positive 但連賣』誤解。"""
    s = streak
    if s is not None and s >= 2:
        return f"法人連買{int(s)}日"
    if s is not None and s <= -2:
        base = f"法人連賣{int(abs(s))}日"
        return f"今日買超｜{base}" if chip_status == "positive" else base
    if chip_status == "positive":
        return "法人今日買超"
    if chip_status == "negative":
        return "法人今日賣超"
    if chip_status == "pending":
        return "法人待補"
    return "法人中性"


def _item_explain(it):
    """入選/觀察列的一句白話(語意層):tier + 強項(layered_reasons) + 風險(layered_risks)。
    純翻譯已算好的分層結果,不做篩選、不吃分數。取代前端印 reasons 原文(融資增/收破月線)。"""
    tier = it.get("tier") or ""
    strengths = it.get("layered_reasons") or []
    risks = list(it.get("layered_risks") or [])
    _s = it.get("inst_streak")
    if it.get("chip_status") == "positive" and _s is not None and _s <= -2:
        risks.append(f"惟法人連賣{int(abs(_s))}日(今日翻買)")
    parts = []
    if strengths:
        parts.append("、".join(strengths[:3]))
    if risks:
        parts.append("但" + "、".join(risks[:2]))
    body = ",".join(parts) if parts else "結構成立、細節待補"
    action = _TIER_ACTION.get(tier, "續觀察。")
    prefix = f"{tier}:" if tier else ""
    return f"{prefix}{body} → {action}"


def _purge_date(table: str, d, db_path: str) -> None:
    """刪某交易日該表所有列(僅 screen_post 自有可變池表 candidate_pool/dropped_pool)。
    治『同一天多次 build,舊 build 落選碼殘留、顯示缺籌碼/錯 chip』。"""
    with store.conn(db_path) as _c:
        _c.execute(f"DELETE FROM {table} WHERE data_date=?", (d.isoformat(),))
        _c.commit()


def load_dropped(data_date: _dt.date | None = None, db_path: str = "mls.db") -> list[dict]:
    """讀某交易日被淘汰名單(真結構失效),映成顯示端「被篩掉名單」需要的欄位。
    說明照結構失效原因走(非分數低);今日漲跌/資金流由前端併入。"""
    d = data_date or prev_trading_day()
    rows = store.read_date(DROPPED_TABLE, d, db_path)
    name_map = _name_map()
    cg = _code_group()
    out = []
    for code, r in rows.items():
        p = json.loads(r["payload"])
        out.append(_shape_dropped_row(code, p, name_map, cg))
    return out


def load_last_post(db_path: str = "mls.db") -> dict:
    """盤中/盤前的「盤後」顯示:攤出上一交易日『已定案』的盤後池 + 入選理由。

    盤後是顯示、不是重算模式。今天盤中還沒收盤,build(today) 會讓收盤/法人/融資全 NO_DATA,
    正是使用者看到的「資料待補、資金流沒勁」。A一 的語意是「對照昨天篩的、看今天怎麼走」,
    所以這裡固定讀 prev_trading_day 那份定案結果,帶 reasons。
    """
    y = prev_trading_day()
    rows = store.read_date(POOL_TABLE, y, db_path)
    items = [json.loads(r["payload"]) for r in rows.values()]
    items.sort(key=lambda x: x.get("rank", 999))
    applies = next_trading_day(y).isoformat()   # = 今天
    return {
        "phase": "POST", "data_date": y.isoformat(), "applies_date": applies,
        "purpose": (f"盤後篩選結果(資料日 {y})— 已定案,{len(items)} 檔候選;"
                    f"適用今日 {applies} 盤中對照觀察,非重算"),
        "actionable": False,
        "generated_at": next(iter(rows.values()))["generated_at"] if rows else None,
        "degraded": [] if items else ["昨日盤後候選池尚未產生"],
        "items": items,
        "dropped": load_dropped(y, db_path),
    }


def load_for_premarket(db_path: str = "mls.db") -> dict:
    """盤前開機:直接讀昨日盤後候選池,不重算、不重抓、零 API,秒開。"""
    y = prev_trading_day()
    rows = store.read_date(POOL_TABLE, y, db_path)
    items = [json.loads(r["payload"]) for r in rows.values()]
    items.sort(key=lambda x: x.get("rank", 999))
    return {
        "phase": "PRE", "data_date": y.isoformat(), "applies_date": today_tw().isoformat(),
        "purpose": f"適用今日 {today_tw()} 盤中(資料日 {y} 盤後產出)— 開盤後只盯這些,非進場名單",
        "actionable": False,
        "generated_at": next(iter(rows.values()))["generated_at"] if rows else None,
        "degraded": [] if items else ["昨日盤後候選池尚未產生"],
        "items": items,
        "dropped": load_dropped(y, db_path),
    }
