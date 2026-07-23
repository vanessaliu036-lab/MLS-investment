"""
screen_intraday.py — 盤中篩選(獨立計分邏輯之一)

回答的問題:現在這一刻,誰在被吸籌?
你的動作:只觀察、記錄。不作進場依據。

資料來源(只有兩個,沒有第三個):
  1. Shioaji 訂閱串流 —— 價、量、主動買賣差、內外盤、當日漲跌幅
  2. DB 裡 data_date=昨日 的死值 —— 法人、融資、MA20/MA60、承接品質

盤中一次都不打 FinMind。所有法人/融資欄位都是昨日死值,早就躺在 DB 裡。
把 FinMind API key 清空,盤中名單照樣出 —— 這是驗收條件。

計分規則:
  - 固定 51 檔全跑,不預先剔除。每檔都有分數,低分自然沉底。
  - 缺資料 = 那一項 0 分,不扣分。NO_DATA 絕不等於 FAIL。
  - 缺什麼寫在 missing[],前端顯示「缺:MA20」。
  - 風險封頂:跌破月線 / 量價背離 → 分數上限壓 60,排不進前 10。

與 screen_post 完全不共用計分函式。這支改壞不影響那支。
"""

from __future__ import annotations

import datetime as _dt
import json

import store
from envelope import Envelope, run_all, persist_status, missing_labels
from phase import Phase, prev_trading_day, today_tw

PLUGIN = "screen_intraday"
TABLE = "watchlist_intraday"

# 權重。加總 100,缺項不補、不重新歸一 —— 缺就是拿不到那項的分。
W = {
    "money_health": 30,   # 資金健康度:全系統核心主軸,權重最高
    "net_active": 22,     # 盤中主動買賣差(非法人買賣超)
    "absorption": 18,     # 承接品質 / 吸籌強度
    "vs_ma20": 12,        # 現價 vs 昨日 MA20
    "inst_streak": 10,    # 昨日法人連買天數
    "margin": 8,          # 昨日融資增減:減=好(散戶洗掉),增=壞
}

RISK_CAP = 60.0


def _norm(x: float, lo: float, hi: float) -> float:
    if x is None:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) if hi > lo else 0.0


def score_one(code: str, quote: dict | None, aflow: dict | None,
              bar_y: dict | None, inst_y: dict | None, margin_y: dict | None,
              health: dict | None, absorb: dict | None) -> dict:
    """
    單檔計分。任何一個參數為 None 都不會爆,只是該項 0 分。
    """
    pts = 0.0
    reasons: list[str] = []
    missing: list[str] = []
    capped = False

    # 資金健康度
    if health and health.get("score") is not None:
        v = _norm(health["score"], 0, 100)
        pts += W["money_health"] * v
        if v > 0.6:
            reasons.append("資金健康度佳")
    else:
        missing.append("資金健康度")

    # 主動買賣差(盤中推估,不是法人買賣超)
    if aflow and aflow.get("net_active") is not None:
        na = aflow["net_active"]
        v = _norm(na, 0, 50_000_000)
        pts += W["net_active"] * v
        if na > 0:
            reasons.append("主動買超")
    else:
        missing.append("主動買賣差")

    # 承接品質
    if absorb and absorb.get("score") is not None:
        v = _norm(absorb["score"], 0, 100)
        pts += W["absorption"] * v
        if v > 0.6:
            reasons.append("承接強")
    else:
        missing.append("承接品質")

    # 現價 vs 昨日 MA20
    price = (quote or {}).get("price")
    ma20 = (bar_y or {}).get("ma20")
    if price is not None and ma20:
        if price >= ma20:
            pts += W["vs_ma20"]
            reasons.append("站上月線")
        else:
            capped = True
            reasons.append("跌破月線")
    else:
        missing.append("MA20")   # 沒接入 = 不計分,絕不踢去排除

    # 法人連買(昨日死值)
    if inst_y and inst_y.get("consecutive_days") is not None:
        d = inst_y["consecutive_days"]
        pts += W["inst_streak"] * _norm(d, 0, 5)
        if d >= 2:
            reasons.append(f"法人連買{d}日")
    else:
        missing.append("法人")

    # 融資增減:減=加分(散戶被洗掉),增=扣分。反直覺但正確。
    if margin_y and margin_y.get("margin_change") is not None:
        ch = margin_y["margin_change"]
        if ch < 0:
            pts += W["margin"]
            reasons.append("融資減(籌碼乾淨)")
        elif ch > 0:
            pts -= W["margin"] * 0.5
            reasons.append("融資增")
    else:
        missing.append("融資")

    # 量價背離:漲但量縮
    cr = (quote or {}).get("change_rate")
    vol = (quote or {}).get("volume")
    vma = (bar_y or {}).get("vol_ma20")
    if cr is not None and vol is not None and vma:
        if cr > 3 and vol < vma * 0.7:
            capped = True
            reasons.append("量價背離")

    score = max(0.0, min(100.0, pts))
    if capped:
        score = min(score, RISK_CAP)

    return {
        "code": code,
        "score": round(score, 1),
        "price": price,
        "change_rate": cr,
        "reasons": reasons,
        "missing": missing,
        "risk_capped": capped,
        "has_data": quote is not None,
    }


def build(universe: list[str], db_path: str = "mls.db") -> dict:
    """
    產出盤中名單。固定 51 檔全集,不是 broker buffer 子集。
    任何一格資料缺失都不會導致名單出不來。
    """
    # 這支從頭到尾只讀 SQLite,不呼叫任何取數函式 —— 所以不需要守門。
    # 守門(assert_can_fetch_finmind)裝在 FinMind 取數函式那一端,不裝在這裡。
    today = today_tw()
    yday = prev_trading_day()

    # 每個插件各自獨立讀取,互不相干。讀共用完全沒問題。
    envs = run_all({
        "quote": lambda: store.read_date("quote_snap", today, db_path),
        "aflow": lambda: store.read_date("aflow", today, db_path),
        "bar_y": lambda: store.read_date("daily_bar", yday, db_path),
        "inst_y": lambda: store.read_date("inst_flow", yday, db_path),
        "margin_y": lambda: store.read_date("margin", yday, db_path),
        "health": lambda: store.read_date("money_health", today, db_path),
        "absorb": lambda: store.read_date("absorption", today, db_path),
    }, phase=Phase.INTRADAY)

    persist_status(envs, db_path)

    q = envs["quote"].get({}) or {}
    a = envs["aflow"].get({}) or {}
    b = envs["bar_y"].get({}) or {}
    i = envs["inst_y"].get({}) or {}
    m = envs["margin_y"].get({}) or {}
    h = envs["health"].get({}) or {}
    ab = envs["absorb"].get({}) or {}

    items = [
        score_one(c, q.get(c), a.get(c), b.get(c), i.get(c), m.get(c),
                  h.get(c), ab.get(c))
        for c in universe
    ]
    items.sort(key=lambda x: (-x["score"], x["code"]))
    for n, it in enumerate(items, 1):
        it["rank"] = n

    gen = _dt.datetime.now().isoformat(timespec="seconds")
    store.upsert_intraday(TABLE, PLUGIN, [{
        "data_date": today.isoformat(), "code": it["code"],
        "rank": it["rank"], "score": it["score"],
        "payload": json.dumps(it, ensure_ascii=False), "generated_at": gen,
    } for it in items], db_path)

    return {
        "phase": "INTRADAY",
        "data_date": today.isoformat(),
        "background_date": yday.isoformat(),
        "purpose": f"盤中吸籌觀察(背景資料日 {yday})— 僅供記錄,不作進場依據",
        "actionable": False,
        "generated_at": gen,
        "degraded": missing_labels(envs),   # 哪幾格壞了,名單照出
        "items": items,                      # 固定 51 檔全集
    }
