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
import hashlib as _hashlib

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
    sector_level_evidence TEXT,
    stock_level_evidence TEXT,
    frozen_signal_name TEXT, frozen_signal_version TEXT, conditioning_version TEXT,
    sector_id TEXT, sector_map_version TEXT, raw_sector_signal REAL,
    -- ── 六項原始指標(章程第 10 條,T+10 口徑)──
    p_hit_3pct REAL, expected_upside REAL, expected_downside REAL,
    net_positive_rate REAL, profit_factor REAL, net_expectancy REAL,
    avg_win REAL, avg_loss REAL, mfe_given_hit REAL, trailing_n INTEGER,
    -- DISPLAY_ONLY(unconditional 全歷史)。不得參與分層。
    disp_p_hit_3pct REAL, disp_expected_upside REAL, disp_expected_downside REAL,
    disp_net_positive_rate REAL, disp_profit_factor REAL, disp_net_expectancy REAL,
    disp_n INTEGER,
    stats_basis TEXT,                  -- per_stock_unconditional / _conditional_on_signal
    stats_usage TEXT,                  -- DISPLAY_ONLY / TIERING / DESCRIPTIVE_ONLY
    stats_conditioning TEXT,           -- unconditional / conditional_on_frozen_signal
    stock_level_available INTEGER,     -- 個股層(conditional)指標是否足以分層
    sector_opportunity INTEGER,        -- 族群層訊號是否觸發
    -- ── 不可變稽核欄位:snapshot 寫入後不得回頭重算 ──
    score_date TEXT,                   -- 計分當日
    history_max_date TEXT,             -- 當時 sidecar 可見的最新歷史日
    outcome_matured_through TEXT,      -- 最後一個「已完整走完 horizon」的樣本進場日
    sidecar_build_id TEXT,             -- 當時使用的 sidecar 版本
    stats_sample_n INTEGER,            -- 分層所用 conditional 統計的樣本數
    -- ── 實際結果(到期回填,這才是 live 驗證的依據)──
    entry_open REAL,
    actual_mfe_t10 REAL, actual_mae_t10 REAL, actual_term_t10 REAL, actual_hit_t10 INTEGER,
    actual_mfe_t15 REAL, actual_mae_t15 REAL, actual_term_t15 REAL, actual_hit_t15 INTEGER,
    snapshot_hash TEXT,                -- 稽核輸入 + 指標的指紋,用於同日重跑比對
    score_version TEXT, created_at TEXT,
    PRIMARY KEY (data_date, code)
);
"""


def ensure(db_path: str = "mls.db") -> None:
    with store.conn(db_path) as c:
        c.executescript(DDL)


class RetroactiveWriteRefused(RuntimeError):
    """試圖覆寫**較舊**日期的 snapshot —— 歷史快照一旦寫入即凍結。"""


class SnapshotMutationRefused(RuntimeError):
    """同日重跑,但輸入(sidecar 版本 / 歷史截止日 / 指標)已經改變。

    不得靜默覆寫 —— 那會讓當天的 live 樣本被事後修改。
    """


# ── 完整 semantic payload(canonical,順序固定)────────────────────
# ⚠ 只 hash tier + 六項指標**不夠**:底層 frozen signal 判定變了、
#   sector mapping 改版、scorer 改版,但 tier 恰好沒變時,hash 會相同 →
#   假 no-op,歷史就被偷偷重寫了。因此把「決定這張快照語意」的東西全部納入。
#   execution timestamp(created_at)刻意排除,否則永遠判不出 idempotent。
_HASH_KEYS = (
    "data_date", "code",
    # 訊號身分與版本
    "frozen_signal_name", "frozen_signal_version", "conditioning_version",
    "sector_id", "sector_map_version",
    "sector_opportunity", "raw_sector_signal", "sector_rank_pct",
    "pa_stage",
    # 分層結果
    "tier", "tier_reasons",
    # 六項指標
    "p_hit_3pct", "expected_upside", "expected_downside",
    "net_positive_rate", "profit_factor", "net_expectancy",
    # 統計性質與樣本
    "stats_sample_n", "stats_basis", "stats_conditioning", "stats_usage",
    "stock_level_available",
    # 證據等級
    "sector_level_evidence", "stock_level_evidence", "evidence_level",
    # as-of 稽核
    "history_max_date", "outcome_matured_through", "sidecar_build_id",
    # 程式版本
    "score_version",
)


def _row_hash(row: dict) -> str:
    """完整 semantic snapshot payload 的 canonical hash。

    任何語意改變(含 signal 改版 / sector mapping 改版 / scorer 改版)
    都會讓 hash 不同 → SnapshotMutationRefused,不可能靜默重寫歷史。
    """
    payload = "\n".join(f"{k}={row.get(k)!r}" for k in _HASH_KEYS)
    return _hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_snapshot(data_date: _dt.date, scored: list[dict],
                   db_path: str = "mls.db") -> int:
    """寫入當日 snapshot —— **append-only**。

    語意(2026-08-24 定案):
      1. **新日期照常新增**。已有 8/24 不得阻擋 8/25、8/26 寫入。
      2. **舊日期不可變**:(code, score_date) 已存在就永不以新的歷史資料覆寫。
      3. **同日重跑**:
         · 稽核輸入與指標完全相同 → idempotent NO-OP(不寫、不報錯)
         · sidecar_build_id / history_max_date / 指標有變 →
           raise SnapshotMutationRefused,**不靜默覆寫**
      理由:live validation 的價值全在「當時看到什麼就是什麼」。
      一旦允許事後改寫,整條 evidence chain 就不再是前瞻的。
    """
    ensure(db_path)
    now = _dt.datetime.now().isoformat(timespec="seconds")
    d = data_date.isoformat()
    with store.conn(db_path) as c:
        newest = c.execute(f"SELECT MAX(data_date) FROM {TABLE}").fetchone()[0]
    if newest and d < newest:
        raise RetroactiveWriteRefused(
            f"拒絕回溯覆寫:要寫 {d},但表中已有更新的 {newest}。"
            f"快照一旦寫入即凍結,sidecar 更新不得回頭重算。"
            f"(新增更新的日期不受此限)")
    now = _dt.datetime.now().isoformat(timespec="seconds")
    rows = []
    for r in scored:
        # 六項指標欄位存 **conditional**(即分層依據);unconditional 另存 display 欄
        t10 = r.get("conditional_stats_t10") or {}
        d10 = r.get("display_stats_t10") or {}
        rows.append({
            "data_date": data_date.isoformat(), "code": str(r["code"]),
            "in_top_sector": int(bool(r.get("signal_in_top_sector"))),
            "sector_rank_pct": r.get("sector_rank_pct"),
            "pa_stage": r.get("pa_stage"), "tier": r.get("tier"),
            "tier_reasons": " / ".join(r.get("tier_reasons") or []),
            "evidence_level": r.get("evidence_level"),
            "sector_level_evidence": r.get("sector_level_evidence"),
            "stock_level_evidence": r.get("stock_level_evidence"),
            "frozen_signal_name": r.get("frozen_signal_name"),
            "frozen_signal_version": r.get("frozen_signal_version"),
            "conditioning_version": r.get("conditioning_version"),
            "sector_id": r.get("sector_id"),
            "sector_map_version": r.get("sector_map_version"),
            "raw_sector_signal": r.get("raw_sector_signal"),
            "p_hit_3pct": t10.get("p_hit_3pct"),
            "expected_upside": t10.get("expected_upside"),
            "expected_downside": t10.get("expected_downside"),
            "net_positive_rate": t10.get("net_positive_rate"),
            "profit_factor": t10.get("profit_factor"),
            "net_expectancy": t10.get("net_expectancy"),
            "avg_win": t10.get("avg_win"), "avg_loss": t10.get("avg_loss"),
            "mfe_given_hit": t10.get("mfe_given_hit"), "trailing_n": t10.get("n"),
            # DISPLAY_ONLY:unconditional 全歷史統計。UI 可顯示,
            # 但**不得暗示它決定了分層** —— 那是已被否決的 Static Stock Prior。
            "disp_p_hit_3pct": d10.get("p_hit_3pct"),
            "disp_expected_upside": d10.get("expected_upside"),
            "disp_expected_downside": d10.get("expected_downside"),
            "disp_net_positive_rate": d10.get("net_positive_rate"),
            "disp_profit_factor": d10.get("profit_factor"),
            "disp_net_expectancy": d10.get("net_expectancy"),
            "disp_n": d10.get("n"),
            "stats_basis": t10.get("stats_basis"),
            "stats_usage": t10.get("usage"),
            "stats_conditioning": t10.get("conditioning"),
            "stock_level_available": int(bool(r.get("stock_level_available"))),
            "sector_opportunity": int(bool(r.get("sector_opportunity"))),
            "score_date": r.get("score_date"),
            "history_max_date": r.get("history_max_date"),
            "outcome_matured_through": t10.get("outcome_matured_through"),
            "sidecar_build_id": r.get("sidecar_build_id"),
            "stats_sample_n": t10.get("n"),
            "entry_open": None,
            "actual_mfe_t10": None, "actual_mae_t10": None,
            "actual_term_t10": None, "actual_hit_t10": None,
            "actual_mfe_t15": None, "actual_mae_t15": None,
            "actual_term_t15": None, "actual_hit_t15": None,
            "score_version": r.get("score_version"), "created_at": now,
        })
    if not rows:
        return 0
    for r in rows:
        r["snapshot_hash"] = _row_hash(r)

    # ── 同日重跑:比對指紋,決定 no-op / 拒絕 / 寫入 ──────────────────
    with store.conn(db_path) as c:
        existing = {r[0]: r[1] for r in c.execute(
            f"SELECT code, snapshot_hash FROM {TABLE} WHERE data_date=?", (d,))}
    if existing:
        changed = [r["code"] for r in rows
                   if r["code"] in existing and existing[r["code"]] != r["snapshot_hash"]]
        if changed:
            raise SnapshotMutationRefused(
                f"{d} 已有快照,但重跑結果與原始不同({len(changed)} 檔,"
                f"例如 {changed[:5]})。sidecar 版本或歷史截止日已改變 —— "
                f"不得靜默覆寫當天的 live 樣本。要重建請先明確刪除該日資料。")
        new_rows = [r for r in rows if r["code"] not in existing]
        if not new_rows:
            return 0        # 完全相同 → idempotent no-op
        rows = new_rows

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
