"""Opportunity forward logging —— 2026-08-24 起累積真正乾淨的 forward data。

⚠ 為什麼必須每天寫:歷史資料(2020-2025)已經大量被看過,frozen signal
   `sec_rs_10d @ Top10%` 雖然在 2020-2023 獨立窗 replicated,但那是回溯式
   held-out。**唯一沒有回看偏誤的最終確認,只能靠 2026-08-24 之後的
   forward data。** 每漏一天就少一天乾淨樣本。

只寫當下事實與凍結訊號狀態,不寫任何預測分數。
T+10 / T+15 的實際結果由 backfill() 到期後自動回填。
"""
from __future__ import annotations
import datetime as _dt

import store
import opportunity_score as osc

TABLE = "opportunity_snapshot"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    data_date TEXT NOT NULL,           -- 快照日 T0(盤後)
    code TEXT NOT NULL,
    -- ── 凍結訊號狀態 ──
    in_top_sector INTEGER,             -- sec_rs_10d 族群排名是否 Top10%
    sector_rank_pct REAL,
    pa_stage TEXT,
    tier TEXT,                         -- PRIMARY / HIGH_POTENTIAL / WATCH / AVOID
    tier_reasons TEXT,
    evidence_level TEXT,
    -- ── 六項原始指標(章程第 10 條,T+10 口徑)──
    p_hit_3pct REAL, expected_upside REAL, expected_downside REAL,
    net_positive_rate REAL, profit_factor REAL, net_expectancy REAL,
    avg_win REAL, avg_loss REAL, mfe_given_hit REAL, trailing_n INTEGER,
    stats_basis TEXT,                  -- per_stock / INSUFFICIENT_HISTORY
    stock_level_available INTEGER,     -- 個股層指標是否可用
    sector_opportunity INTEGER,        -- 族群層訊號是否觸發
    -- ── 實際結果(到期回填,這才是 live 驗證的依據)──
    entry_open REAL,
    actual_mfe_t10 REAL, actual_mae_t10 REAL, actual_term_t10 REAL, actual_hit_t10 INTEGER,
    actual_mfe_t15 REAL, actual_mae_t15 REAL, actual_term_t15 REAL, actual_hit_t15 INTEGER,
    score_version TEXT, created_at TEXT,
    PRIMARY KEY (data_date, code)
);
"""


def ensure(db_path: str = "mls.db") -> None:
    with store.conn(db_path) as c:
        c.executescript(DDL)


def write_snapshot(data_date: _dt.date, scored: list[dict],
                   db_path: str = "mls.db") -> int:
    ensure(db_path)
    now = _dt.datetime.now().isoformat(timespec="seconds")
    rows = []
    for r in scored:
        t10 = r.get("t10") or {}
        rows.append({
            "data_date": data_date.isoformat(), "code": str(r["code"]),
            "in_top_sector": int(bool(r.get("signal_in_top_sector"))),
            "sector_rank_pct": r.get("sector_rank_pct"),
            "pa_stage": r.get("pa_stage"), "tier": r.get("tier"),
            "tier_reasons": " / ".join(r.get("tier_reasons") or []),
            "evidence_level": r.get("evidence_level"),
            "p_hit_3pct": t10.get("p_hit_3pct"),
            "expected_upside": t10.get("expected_upside"),
            "expected_downside": t10.get("expected_downside"),
            "net_positive_rate": t10.get("net_positive_rate"),
            "profit_factor": t10.get("profit_factor"),
            "net_expectancy": t10.get("net_expectancy"),
            "avg_win": t10.get("avg_win"), "avg_loss": t10.get("avg_loss"),
            "mfe_given_hit": t10.get("mfe_given_hit"), "trailing_n": t10.get("n"),
            "stats_basis": t10.get("stats_basis"),
            "stock_level_available": int(bool(r.get("stock_level_available"))),
            "sector_opportunity": int(bool(r.get("sector_opportunity"))),
            "entry_open": None,
            "actual_mfe_t10": None, "actual_mae_t10": None,
            "actual_term_t10": None, "actual_hit_t10": None,
            "actual_mfe_t15": None, "actual_mae_t15": None,
            "actual_term_t15": None, "actual_hit_t15": None,
            "score_version": r.get("score_version"), "created_at": now,
        })
    if not rows:
        return 0
    cols = list(rows[0])
    ph = ",".join("?" * len(cols))
    with store.conn(db_path) as c:
        cur = c.executemany(
            f"INSERT OR REPLACE INTO {TABLE} ({','.join(cols)}) VALUES ({ph})",
            [tuple(r[k] for k in cols) for r in rows])
        c.commit()
    return cur.rowcount


def backfill(db_path: str = "mls.db") -> int:
    """回填已到期的 T+10 / T+15 實際結果。

    進場價一律 T+1 開盤 —— 盤後名單在 T0 收盤買不到,用收盤會把隔夜跳空
    算成自己的績效(與 pa_snapshot.backfill 同一理由)。
    """
    ensure(db_path)
    n = 0
    with store.conn(db_path) as c:
        pending = c.execute(
            f"SELECT data_date, code FROM {TABLE} WHERE actual_term_t15 IS NULL"
        ).fetchall()
        for data_date, code in pending:
            bars = c.execute(
                "SELECT data_date, open, high, low, close FROM daily_bar "
                "WHERE code=? AND data_date>? ORDER BY data_date LIMIT 16",
                (code, data_date)).fetchall()
            if len(bars) < 2:
                continue
            entry = bars[0][1]
            if not entry:
                continue
            upd = {"entry_open": entry}
            for h in (10, 15):
                win = bars[:h]
                if len(win) < h:
                    continue
                highs = [b[2] for b in win if b[2] is not None]
                lows = [b[3] for b in win if b[3] is not None]
                if not highs or not lows or win[-1][4] is None:
                    continue
                mfe = max(highs) / entry - 1 - osc.COST
                mae = min(lows) / entry - 1 - osc.COST
                term = win[-1][4] / entry - 1 - osc.COST
                upd[f"actual_mfe_t{h}"] = round(mfe * 100, 3)
                upd[f"actual_mae_t{h}"] = round(mae * 100, 3)
                upd[f"actual_term_t{h}"] = round(term * 100, 3)
                upd[f"actual_hit_t{h}"] = int(mfe >= osc.OPPORTUNITY_THRESHOLD)
            if len(upd) <= 1:
                continue
            sets = ",".join(f"{k}=?" for k in upd)
            c.execute(f"UPDATE {TABLE} SET {sets} WHERE data_date=? AND code=?",
                      (*upd.values(), data_date, code))
            n += 1
        c.commit()
    return n


def live_summary(db_path: str = "mls.db", horizon: int = 10) -> dict:
    """live forward 驗證讀出:訊號組 vs 非訊號組的實際 +3% 命中率。

    這是唯一沒有回看偏誤的證據來源。

    ⚠ 樣本未達門檻時**照樣顯示 n 與當下數字**,只是標 DESCRIPTIVE ONLY。
      理由(2026-08-24 定案):如果 live 從第 20 筆就完全反向,我們必須看得見;
      看得見不等於可以據此改 frozen signal —— 那是兩件事。
    """
    ensure(db_path)
    out = {}
    with store.conn(db_path) as c:
        for label, where in (("in_top_sector", "in_top_sector=1"),
                             ("rest", "in_top_sector=0")):
            rows = c.execute(
                f"SELECT actual_hit_t{horizon}, actual_mfe_t{horizon}, "
                f"actual_mae_t{horizon}, actual_term_t{horizon} FROM {TABLE} "
                f"WHERE {where} AND actual_hit_t{horizon} IS NOT NULL").fetchall()
            if not rows:
                out[label] = {"n": 0}
                continue
            hits = [r[0] for r in rows]
            mfes = [r[1] for r in rows if r[1] is not None]
            maes = [r[2] for r in rows if r[2] is not None]
            terms = [r[3] for r in rows if r[3] is not None]
            out[label] = {
                "n": len(rows),
                "p_hit_3pct": round(sum(hits) / len(hits) * 100, 2),
                "expected_upside": round(sum(mfes) / len(mfes), 2) if mfes else None,
                "expected_downside": round(sum(maes) / len(maes), 2) if maes else None,
                "net_expectancy": round(sum(terms) / len(terms), 3) if terms else None,
                "enough": len(rows) >= 100,
                "status": "CONFIRMATORY" if len(rows) >= 100 else "DESCRIPTIVE ONLY",
            }
    out["status"] = ("CONFIRMATORY"
                     if min(out.get("in_top_sector", {}).get("n", 0),
                            out.get("rest", {}).get("n", 0)) >= 100
                     else "DESCRIPTIVE ONLY —— 可以看,不得據此改 frozen signal")
    if out.get("in_top_sector", {}).get("n") and out.get("rest", {}).get("n"):
        out["lift_pp"] = round(out["in_top_sector"]["p_hit_3pct"]
                               - out["rest"]["p_hit_3pct"], 2)
        out["historical_expectation_pp"] = 5.66 if horizon == 10 else 5.87
    return out
