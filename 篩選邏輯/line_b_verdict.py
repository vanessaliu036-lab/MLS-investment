"""line_b_verdict.py — Line B C1+C2 樣本的 T 日收盤 verdict(唯讀,不寫 DB)。

⚠ 跟 line_b_watch_ledger / run_line_b_ledger / line_b_layers 完全獨立:
   只讀 line_b_watch_ledger 已落地的列,不改寫任何欄位,不影響任何既有
   tier/track/score/gate。Vanessa 2026-08-27 定案的流程:

       T-1 C1+C2 入組 → T 日盤中 forward data → T 日收盤 verdict
       → SUCCESS / FAIL / NO_TRIGGER / INCOMPLETE → 重新計算歷史啟動率

   只對 source="C1C2_PASS" 的列判 verdict —— INTRADAY_DISCOVERY 是另一個
   cohort(當天才冒出來,T-1 沒有入組資格),不能混進同一組統計。

═══ 四態定義(只用該列已經存在的 T 日收盤欄位,不需要 T+1 資料)═══
  INCOMPLETE  flow_class 是 None                — 當天沒有可用的盤中時序
              (跟「沒觸發」不同,不能算 NO_TRIGGER)
  NO_TRIGGER  有盤中時序,但 watch_mode_activated=0 — 沒觸發 confirmed_reversal
  SUCCESS     watch_mode_activated=1 且 t_close >= max(t1_ma20, t1_prior_high)
              — 觸發後收盤仍守住觸發參考位
  FAIL        watch_mode_activated=1 且 t_close <  max(t1_ma20, t1_prior_high)
              — 觸發後收盤又跌破,當天是假突破

  參考位跟 decision_view._entry_state() 判斷 triggered/trigger_failed 用的
  同一組邏輯(close 是否守住 prior_high/MA20),故意對齊,不另外發明一套。

═══ 誠實標註 ═══
  這支存在的目的是「開始累積」,不是「現在就有結論」。樣本天數不夠時,
  n 一律隨數字回傳,不讓它看起來比實際可靠 —— 這是 line_b_layers.py 已經
  用過的同一條規矩。舊的 64.1%/89.9%/2.8% 是 2026-08-26 封板時的離線
  retrospective 數字,量的是「有沒有觸發」;這支新算的是「觸發後收盤有沒有
  守住」,兩者定義不同,不能直接比大小,只能各自看自己隨時間的變化。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

DB = "mls.db"

VERDICT_SUCCESS = "SUCCESS"
VERDICT_FAIL = "FAIL"
VERDICT_NO_TRIGGER = "NO_TRIGGER"
VERDICT_INCOMPLETE = "INCOMPLETE"


def verdict_for(row: dict) -> str:
    """單一列(source 必須是 C1C2_PASS)的 T 日收盤 verdict。"""
    if row.get("flow_class") is None:
        return VERDICT_INCOMPLETE
    if not row.get("watch_mode_activated"):
        return VERDICT_NO_TRIGGER
    ma20, prior_high = row.get("t1_ma20"), row.get("t1_prior_high")
    close = row.get("t_close")
    refs = [v for v in (ma20, prior_high) if v is not None]
    if close is None or not refs:
        return VERDICT_INCOMPLETE
    return VERDICT_SUCCESS if close >= max(refs) else VERDICT_FAIL


def _rate_block(rows: list[dict]) -> dict:
    n = len(rows)
    counts = {VERDICT_SUCCESS: 0, VERDICT_FAIL: 0, VERDICT_NO_TRIGGER: 0, VERDICT_INCOMPLETE: 0}
    for r in rows:
        counts[verdict_for(r)] += 1
    triggered = counts[VERDICT_SUCCESS] + counts[VERDICT_FAIL]
    return {
        "n": n,
        "counts": counts,
        "activation_rate": round(triggered / n * 100, 1) if n else None,
        "hold_rate_given_triggered": (round(counts[VERDICT_SUCCESS] / triggered * 100, 1)
                                      if triggered else None),
    }


def cumulative_confirmed_rates(db_path: str = DB) -> dict:
    """Calculate the cumulative hit rate for C2 + confirmed A-flow.

    This is intentionally expanding, not a fixed rolling window: every new
    settled trading day increases the sample instead of dropping the oldest
    day. A hit means watch_mode_activated=1 for a C1C2_PASS row whose C2 flag
    is true and whose flow class is OPEN_POSITIVE or FLOW_FLIP.
    """
    empty = {
        "status": "CUMULATIVE",
        "n_days": 0,
        "data_dates": [],
        "n": 0,
        "hit_count": 0,
        "no_hit_count": 0,
        "hit_rate_pct": None,
        "hit_definition": "C1+C2_PASS + OPEN_POSITIVE/FLOW_FLIP + watch_mode_activated",
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='line_b_watch_ledger'"
        ).fetchone()
        if table is None:
            return empty
        rows = [dict(r) for r in conn.execute(
            "SELECT data_date,watch_mode_activated FROM line_b_watch_ledger "
            "WHERE source='C1C2_PASS' AND c2_selling_weak_price_resp=1 "
            "AND flow_class IN ('OPEN_POSITIVE','FLOW_FLIP') "
            "ORDER BY data_date,code"
        ).fetchall()]
    finally:
        conn.close()

    dates = sorted({r["data_date"] for r in rows})
    hits = sum(1 for r in rows if r["watch_mode_activated"])
    empty.update(
        n_days=len(dates), data_dates=dates, n=len(rows), hit_count=hits,
        no_hit_count=len(rows) - hits,
        hit_rate_pct=round(hits / len(rows) * 100, 1) if rows else None,
    )
    return empty


def forward_rates(db_path: str = DB, since: Optional[str] = None) -> dict:
    """逐日累積重算(day-equal:每天先各自算一次,再看趨勢,不做加權平均掩蓋單日爆量)。
    只吃 source=C1C2_PASS 的列。since 為 None 時用全部已累積的交易日。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        q = "SELECT * FROM line_b_watch_ledger WHERE source='C1C2_PASS'"
        params: list[str] = []
        if since:
            q += " AND data_date >= ?"
            params.append(since)
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    finally:
        conn.close()

    by_date: dict[str, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["data_date"], []).append(r)

    daily = []
    for d in sorted(by_date):
        block = _rate_block(by_date[d])
        block["data_date"] = d
        daily.append(block)

    by_flow: dict[str, list[dict]] = {}
    for r in rows:
        by_flow.setdefault(r.get("flow_class") or "NULL", []).append(r)
    flow_breakdown = {k: _rate_block(v) for k, v in sorted(by_flow.items())}

    overall = _rate_block(rows)
    return {
        "definition_version": "line_b_verdict_v1_2026-08-27",
        "n_days": len(by_date),
        "overall": overall,
        "by_flow_class": flow_breakdown,
        "daily": daily,
        "caveat": ("n_days 太少時這些比率不是穩定估計,只是累積進度。"
                  "跟舊的 64.1%/89.9%/2.8%(封板離線數字,量的是「有沒有觸發」)"
                  "不是同一個定義,不能直接比較，只能各自看自己隨時間的變化。"),
    }
