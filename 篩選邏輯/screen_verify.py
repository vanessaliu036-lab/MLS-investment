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
    verified_at TEXT,
    PRIMARY KEY (data_date, code)
);
"""


def _ensure_table(db_path: str = "mls.db") -> None:
    with store.conn(db_path) as c:
        c.executescript(_DDL)
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
    }, phase=Phase.POST)
    persist_status(envs, db_path)

    pool = envs["pool"].get({}) or {}
    bar = envs["bar"].get({}) or {}

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
            "verdict": verdict, "verified_at": now,
        }
        rows.append(rec)
        items.append(rec)

    store.upsert_intraday(TABLE, PLUGIN, rows, db_path)

    scored = [r for r in items if r["hit"] is not None]     # 攻擊+引擎(有資料)
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
        "ret_median": median,
        "ret_avg": round(sum(rets) / len(rets), 2) if rets else None,
        "items": items,
    }


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
    return {
        "window_days": days, "since": since,
        "overall_hit_rate": round(total_hits / total_scored * 100, 1) if total_scored else None,
        "scored": total_scored, "hits": total_hits,
        "by_track": by_track, "daily": daily,
    }
