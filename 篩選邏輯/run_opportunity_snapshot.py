#!/usr/bin/env python3
"""Opportunity forward logging 每日排程(2026-08-24 起)。

⚠ 為什麼必須每天跑:frozen signal `sec_rs_10d @ Top10%` 在 2020-2023 獨立窗
   已 replicated,但那是回溯式 held-out。**唯一沒有回看偏誤的最終確認,只能靠
   2026-08-24 之後的 forward data。每漏一天就少一天乾淨樣本。**

只套用凍結定義、只寫當下事實,不做任何 discovery、不擬合任何模型。
同一天重跑安全:write_snapshot 用 INSERT OR REPLACE,backfill 只補未到期的列。
"""
from __future__ import annotations
import datetime as _dt
import os
import sys

import config
import store
import opportunity_score as osc
import opportunity_snapshot as osnap

# 當日 daily_bar 覆蓋率門檻。與 run_pa_snapshot 同一理由:
# live observation 是不能污染的前瞻樣本,寧可缺一天也不要混入半真樣本。
MIN_BAR_COVERAGE = float(os.environ.get("OPP_MIN_BAR_COVERAGE", "1.0"))
HISTORY_DAYS = 400          # 個股統計需要約一年 + T+15 到期緩衝


def _latest_trading_date(db_path: str = "mls.db") -> _dt.date | None:
    with store.conn(db_path) as c:
        row = c.execute("SELECT MAX(data_date) FROM daily_bar").fetchone()
    return _dt.date.fromisoformat(row[0]) if row and row[0] else None


def main() -> int:
    try:
        d = _latest_trading_date()
        if d is None:
            print("[opportunity] daily_bar 無資料,略過", flush=True)
            return 1

        codes = [str(c) for c in config.UNIVERSE]
        # ⚠ store.read_recent 回傳「由新到舊」;本模組全部假設「由舊到新」
        #   (realized_opportunity_stats 的視窗、sector_rs_10d 的 idx 都是)。
        #   不反轉會靜默算出反向統計 —— 2026-08-24 首次上線時被覆蓋率護欄擋下。
        bars = {c: list(reversed(store.read_recent("daily_bar", c, d, HISTORY_DAYS)))
                for c in codes}

        have_today = sum(1 for c in codes if bars[c]
                         and str(bars[c][-1].get("data_date")) == d.isoformat())
        need = max(1, int(len(codes) * MIN_BAR_COVERAGE))
        if have_today < need:
            print(f"[opportunity] ✋ 拒寫:{d} daily_bar 只有 {have_today}/{len(codes)} 檔"
                  f"(門檻 {need}),資料未補齊,不寫半真快照。"
                  f"補齊後重跑本支即可(INSERT OR REPLACE)。", flush=True)
            return 2

        # ── 依族群組收盤序列,算 leave-one-out 的 sec_rs_10d ──────────
        by_sector: dict[str, dict[str, list]] = {}
        for c in codes:
            sec = config.CODE_GROUP.get(c, "其他")
            closes = [b.get("close") for b in bars[c]]
            by_sector.setdefault(sec, {})[c] = closes

        rs_by_code: dict[str, float | None] = {}
        for sec, members in by_sector.items():
            for c, seq in members.items():
                idx = len(seq) - 1
                rs_by_code[c] = osc.sector_rs_10d(members, c, idx)

        # ── 族群中位數排名(同族群同排名,才是族群層決策)──────────────
        sec_vals: dict[str, list[float]] = {}
        for c, v in rs_by_code.items():
            if v is not None:
                sec_vals.setdefault(config.CODE_GROUP.get(c, "其他"), []).append(v)
        sec_median = {s: sorted(v)[len(v) // 2] for s, v in sec_vals.items() if v}
        ordered = sorted(sec_median.items(), key=lambda kv: kv[1])
        n_sec = len(ordered)
        sec_rank = {s: (i + 1) / n_sec for i, (s, _) in enumerate(ordered)} if n_sec else {}

        # ── Pre-Activation stage(唯讀併入,盤後已算好)────────────────
        stage_by_code: dict[str, str | None] = {}
        try:
            import json
            with store.conn("mls.db") as c:
                for (payload,) in c.execute(
                        "SELECT payload FROM candidate_pool WHERE data_date=?",
                        (d.isoformat(),)):
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    st = (obj.get("pre_activation") or {}).get("stage")
                    if obj.get("code"):
                        stage_by_code[str(obj["code"])] = st
        except Exception as exc:
            print(f"[opportunity] stage 併入略過(不影響本體): {exc}", flush=True)

        scored = []
        for c in codes:
            sec = config.CODE_GROUP.get(c, "其他")
            scored.append(osc.score_one(
                c, bars[c], by_sector.get(sec, {}),
                sec_rank.get(sec), stage_by_code.get(c)))

        n = osnap.write_snapshot(d, scored)
        b = osnap.backfill()

        tiers: dict[str, int] = {}
        for r in scored:
            tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
        top = sum(1 for r in scored if r["signal_in_top_sector"])
        print(f"[opportunity] {d} 快照 {n} 列 / 回填 {b} 列 / "
              f"族群 Top10% {top} 檔 / 分層 {tiers}", flush=True)
        if n == 0:
            print("[opportunity] ⚠ 一列都沒寫入", flush=True)
            return 1
        return 0
    except Exception as e:
        print(f"[opportunity] 失敗: {type(e).__name__}: {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
