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
from envelope import run_all, persist_status, missing_labels
from phase import Phase, prev_trading_day, today_tw

PLUGIN = "screen_post"
TABLE = "watchlist_post"
POOL_TABLE = "candidate_pool"   # 隔日盤中只盯這張表

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

def assign_track(bar: dict | None, item: dict) -> dict:
    close = (bar or {}).get("close")
    high = (bar or {}).get("high")
    ma20 = (bar or {}).get("ma20")
    vol = (bar or {}).get("volume")
    vma = (bar or {}).get("vol_ma20")

    on_ma20 = close is not None and ma20 and close >= ma20
    volumed = vol is not None and vma and vol >= vma * 1.2

    if on_ma20 and volumed:
        item["track"] = "攻擊軌"
        item["entry_rule"] = f"等突破昨高 {high},ATR 停損"
        item["trigger_price"] = high
    elif on_ma20:
        item["track"] = "引擎軌"
        item["entry_rule"] = f"等回月線 {ma20} 支撐 + 法人買進,月線停損"
        item["trigger_price"] = ma20
    else:
        item["track"] = "觀察"
        item["entry_rule"] = "位置不佳,等盤中訊號,不主動進場"
        item["trigger_price"] = None
    return item


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

    kept, dropped = [], []
    for c in universe:
        drop, hits = hard_drop(b.get(c), i.get(c))
        if drop:
            dropped.append({"code": c, "why": hits})
            continue
        it = score_one(c, b.get(c), i.get(c), m.get(c), h.get(c), ab.get(c))
        kept.append(assign_track(b.get(c), it))

    kept.sort(key=lambda x: (-x["score"], x["code"]))
    pool = kept[:POOL_SIZE]
    for n, it in enumerate(pool, 1):
        it["rank"] = n

    gen = _dt.datetime.now().isoformat(timespec="seconds")

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

    try:
        store.snapshot_post(d, db_path)
    except Exception:
        pass

    tracks: dict[str, int] = {}
    for it in pool:
        tracks[it["track"]] = tracks.get(it["track"], 0) + 1

    return {
        "phase": "POST", "data_date": d.isoformat(),
        "purpose": f"隔日盤中候選池(資料日 {d})— 明天只盯這 {len(pool)} 檔,不是進場名單",
        "actionable": False,
        "generated_at": gen,
        "degraded": missing_labels(envs),
        "universe_size": len(universe),
        "dropped_count": len(dropped),
        "pool_size": len(pool),
        "track_breakdown": tracks,
        "items": pool,
        "dropped": dropped,
    }


def load_pool(data_date: _dt.date | None = None, db_path: str = "mls.db") -> dict[str, dict]:
    """隔天盤中呼叫這支拿候選池。預設讀上一個交易日產出的那份。"""
    d = data_date or prev_trading_day()
    return store.read_date(POOL_TABLE, d, db_path)


def load_for_premarket(db_path: str = "mls.db") -> dict:
    """盤前開機:直接讀昨日盤後候選池,不重算、不重抓、零 API,秒開。"""
    y = prev_trading_day()
    rows = store.read_date(POOL_TABLE, y, db_path)
    items = [json.loads(r["payload"]) for r in rows.values()]
    items.sort(key=lambda x: x.get("rank", 999))
    return {
        "phase": "PRE", "data_date": y.isoformat(),
        "purpose": f"今日盯盤候選池(資料日 {y})— 昨日盤後產出,開盤後只盯這些",
        "actionable": False,
        "generated_at": next(iter(rows.values()))["generated_at"] if rows else None,
        "degraded": [] if items else ["昨日盤後候選池尚未產生"],
        "items": items,
    }
