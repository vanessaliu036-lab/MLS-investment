"""
screen_post.py — 盤後篩選(獨立計分邏輯之二)

回答的問題:明天可以進哪一檔?
你的動作:唯一的下單決策依據,依此執行。

資料來源(全部今日已定案,沒有推估值):
  - 今日法人買賣超(交易所公布,當天定案,永不修改)
  - 今日融資融券增減(同上)
  - 今日收盤 vs MA20 / MA60
  - 今日全天量價結構
  - 今日盤中累積的主動買賣差(收盤後不清空,留在原地當佐證)

與 screen_intraday 完全不共用計分函式。那支改壞不影響這支。

盤前不需要另一套邏輯 —— 盤前就是昨天盤後的結果。
開機直接讀昨日這份名單,不重算、不重抓、零 API。見 load_for_premarket()。
"""

from __future__ import annotations

import datetime as _dt
import json

import store
from envelope import run_all, persist_status, missing_labels
from phase import Phase, prev_trading_day, today_tw

PLUGIN = "screen_post"
TABLE = "watchlist_post"

# 盤後權重與盤中不同:今日法人已定案,權重拉高;盤中推估值降為佐證。
W = {
    "money_health": 28,    # 資金健康度:核心主軸
    "inst_today": 25,      # 今日法人買賣超(已定案,不是推估)
    "inst_streak": 12,     # 法人連買天數
    "vs_ma20": 12,         # 今收 vs MA20
    "margin": 10,          # 今日融資增減
    "absorption": 8,       # 承接品質
    "aflow_confirm": 5,    # 盤中主動買賣差作為佐證(權重最低,它只是推估)
}

RISK_CAP = 60.0


def _norm(x, lo, hi):
    if x is None:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) if hi > lo else 0.0


def score_one(code, bar, inst, margin, health, absorb, aflow) -> dict:
    pts = 0.0
    reasons: list[str] = []
    missing: list[str] = []
    capped = False

    if health and health.get("score") is not None:
        v = _norm(health["score"], 0, 100)
        pts += W["money_health"] * v
        if v > 0.6:
            reasons.append("資金健康度佳")
    else:
        missing.append("資金健康度")

    # 今日法人買賣超 —— 已定案。差幾百張不影響方向判斷,不做交叉驗證。
    if inst and inst.get("total_net") is not None:
        net = inst["total_net"]
        if net > 0:
            pts += W["inst_today"] * _norm(net, 0, 5000)
            reasons.append(f"法人買超{net}張")
        else:
            pts -= W["inst_today"] * 0.4 * _norm(-net, 0, 5000)
            reasons.append(f"法人賣超{-net}張")
    else:
        missing.append("今日法人")

    if inst and inst.get("consecutive_days") is not None:
        d = inst["consecutive_days"]
        pts += W["inst_streak"] * _norm(d, 0, 5)
        if d >= 2:
            reasons.append(f"連買{d}日")
    else:
        missing.append("法人連買天數")

    close = (bar or {}).get("close")
    ma20 = (bar or {}).get("ma20")
    if close is not None and ma20:
        if close >= ma20:
            pts += W["vs_ma20"]
            reasons.append("收在月線上")
        else:
            capped = True
            reasons.append("收破月線")
    else:
        missing.append("MA20")

    if margin and margin.get("margin_change") is not None:
        ch = margin["margin_change"]
        if ch < 0:
            pts += W["margin"]
            reasons.append("融資減(散戶洗出)")
        elif ch > 0:
            pts -= W["margin"] * 0.5
            reasons.append("融資增")
    else:
        missing.append("融資")

    if absorb and absorb.get("score") is not None:
        pts += W["absorption"] * _norm(absorb["score"], 0, 100)
    else:
        missing.append("承接品質")

    # 盤中主動買賣差只作佐證。鐵律:它不是法人買賣超。
    if aflow and aflow.get("net_active") is not None:
        if aflow["net_active"] > 0:
            pts += W["aflow_confirm"]
            reasons.append("盤中主動買超佐證")
    else:
        missing.append("盤中主動買賣差")

    # 量價背離
    vol = (bar or {}).get("volume")
    vma = (bar or {}).get("vol_ma20")
    if close and vol and vma and ma20:
        if vol > vma * 2.5 and close < (bar.get("open") or close):
            capped = True
            reasons.append("爆量收黑")

    score = max(0.0, min(100.0, pts))
    if capped:
        score = min(score, RISK_CAP)

    return {
        "code": code, "score": round(score, 1),
        "close": close, "reasons": reasons, "missing": missing,
        "risk_capped": capped, "has_data": bar is not None,
    }


def build(universe: list[str], db_path: str = "mls.db",
          data_date: _dt.date | None = None) -> dict:
    d = data_date or today_tw()

    envs = run_all({
        "bar": lambda: store.read_date("daily_bar", d, db_path),
        "inst": lambda: store.read_date("inst_flow", d, db_path),
        "margin": lambda: store.read_date("margin", d, db_path),
        "health": lambda: store.read_date("money_health", d, db_path),
        "absorb": lambda: store.read_date("absorption", d, db_path),
        "aflow": lambda: store.read_date("aflow", d, db_path),
    }, phase=Phase.POST)

    persist_status(envs, db_path)

    b = envs["bar"].get({}) or {}
    i = envs["inst"].get({}) or {}
    m = envs["margin"].get({}) or {}
    h = envs["health"].get({}) or {}
    ab = envs["absorb"].get({}) or {}
    af = envs["aflow"].get({}) or {}

    items = [score_one(c, b.get(c), i.get(c), m.get(c), h.get(c), ab.get(c), af.get(c))
             for c in universe]
    items.sort(key=lambda x: (-x["score"], x["code"]))
    for n, it in enumerate(items, 1):
        it["rank"] = n

    gen = _dt.datetime.now().isoformat(timespec="seconds")
    store.upsert_intraday(TABLE, PLUGIN, [{
        "data_date": d.isoformat(), "code": it["code"],
        "rank": it["rank"], "score": it["score"],
        "payload": json.dumps(it, ensure_ascii=False), "generated_at": gen,
    } for it in items], db_path)

    # 名單定案後存指紋,之後有插件動到就報錯
    try:
        store.snapshot_post(d, db_path)
    except Exception:
        pass

    return {
        "phase": "POST", "data_date": d.isoformat(),
        "purpose": f"明日進場清單(資料日 {d})— 依此執行",
        "actionable": True, "generated_at": gen,
        "degraded": missing_labels(envs),
        "items": items,
    }


def load_for_premarket(db_path: str = "mls.db") -> dict:
    """
    盤前開機用。直接讀昨日盤後那份名單,不重算、不重抓、零 API,秒開。

    盤前就是昨天盤後的結果,再算一次是多餘的。
    """
    y = prev_trading_day()
    rows = store.read_date(TABLE, y, db_path)
    items = [json.loads(r["payload"]) for r in rows.values()]
    items.sort(key=lambda x: x.get("rank", 999))
    return {
        "phase": "PRE", "data_date": y.isoformat(),
        "purpose": f"今日盯盤名單(資料日 {y})— 昨日盤後結果,開盤後觀察用",
        "actionable": False,
        "generated_at": next(iter(rows.values()))["generated_at"] if rows else None,
        "degraded": [] if items else ["昨日盤後名單尚未產生"],
        "items": items,
    }
