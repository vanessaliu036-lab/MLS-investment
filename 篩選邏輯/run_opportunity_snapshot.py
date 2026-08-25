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
import opportunity_history as ohist

# 當日 daily_bar 覆蓋率門檻。與 run_pa_snapshot 同一理由:
# live observation 是不能污染的前瞻樣本,寧可缺一天也不要混入半真樣本。
MIN_BAR_COVERAGE = float(os.environ.get("OPP_MIN_BAR_COVERAGE", "1.0"))
HISTORY_DAYS = 400          # 個股統計需要約一年 + T+15 到期緩衝


def _historical_signal_days(hist: dict, codes: list[str]) -> dict:
    """逐日重算 frozen signal(sec_rs_10d 族群中位數排名 Top10%)的歷史觸發日。

    ⚠ 必須用同族群橫斷面逐日算 —— 這是族群層訊號,單檔算不出來。
    ⚠ 一律 leave-one-out(sector_rs_10d 內建),否則是偽裝的個股動能。
    """
    # 對齊所有股票的日期軸
    all_dates = sorted({b["data_date"] for c in codes for b in hist.get(c, [])})
    idx_of = {c: {b["data_date"]: i for i, b in enumerate(hist.get(c, []))} for c in codes}
    closes = {c: [b["close"] for b in hist.get(c, [])] for c in codes}
    out = {c: set() for c in codes}

    for day in all_dates:
        # 當日各族群的 LOO sec_rs_10d 中位數
        per_sector: dict[str, list[float]] = {}
        per_code: dict[str, float] = {}
        for c in codes:
            i = idx_of[c].get(day)
            if i is None or i < 10:
                continue
            sec = config.CODE_GROUP.get(c, "其他")
            # ⚠ 每檔在自己序列中的 index 不同,不能共用同一個 idx。
            #   切出「結束於同一天、長度都是 11」的片段,再統一用 idx=10。
            aligned = {}
            for m in codes:
                if config.CODE_GROUP.get(m, "其他") != sec:
                    continue
                j = idx_of[m].get(day)
                if j is None or j < 10:
                    continue
                aligned[m] = closes[m][j - 10:j + 1]
            if c not in aligned:
                continue
            v = osc.sector_rs_10d(aligned, c, 10)
            if v is None:
                continue
            per_code[c] = v
            per_sector.setdefault(sec, []).append(v)
        if not per_sector:
            continue
        med = {s: sorted(v)[len(v) // 2] for s, v in per_sector.items()}
        order = sorted(med.items(), key=lambda kv: kv[1])
        n = len(order)
        rank = {s: (i + 1) / n for i, (s, _) in enumerate(order)}
        for c in per_code:
            if rank.get(config.CODE_GROUP.get(c, "其他"), 0) > osc.SECTOR_TOP_PCT:
                out[c].add(day)
    return out


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

        # ── 歷史統計一律讀 sidecar,**不把歷史塞進 production DB** ──────
        #   架構:cache → sidecar → 今日 scoring → mls.db 只存結果。
        #   coverage contract 任一項失敗只降級該股票,不讓整條盤後流程失敗。
        cov = ohist.coverage_contract(codes, d.isoformat())
        summ = cov.get("_summary", {})
        if summ.get("store_missing"):
            print(f"[opportunity] ⚠ sidecar 不存在({summ.get('path')}),"
                  f"個股層統計全部降級為 INSUFFICIENT_HISTORY", flush=True)
        else:
            print(f"[opportunity] sidecar coverage {summ['ok_codes']}/{summ['total']} 檔通過"
                  f"(歷史天數 min={summ['min_days']} 中位={summ['median_days']} "
                  f"max={summ['max_days']},{summ['oldest']}~{summ['newest']})", flush=True)
            for c in codes:
                if not cov[c]["ok"]:
                    print(f"[opportunity]   ↓ {c} 降級: {'; '.join(cov[c]['reasons'])}",
                          flush=True)

        hist = {c: (ohist.read_bars(c, d.isoformat(), HISTORY_DAYS)
                    if cov.get(c, {}).get("ok") else [])
                for c in codes}

        # ── frozen signal 的歷史觸發日 ────────────────────────────────
        #   conditional 統計只能用「訊號當日」的樣本。unconditional 全歷史統計
        #   等同 Static Stock Prior(已被 walk-forward 否決),不得用於分層。
        #   訊號是族群層的,必須用同族群橫斷面逐日重算,不能單檔算。
        signal_days = _historical_signal_days(hist, codes)
        sd_n = {c: len(signal_days.get(c, set())) for c in codes}
        print(f"[opportunity] 歷史訊號觸發日 每檔 min={min(sd_n.values())} "
              f"中位={sorted(sd_n.values())[len(sd_n)//2]} max={max(sd_n.values())}",
              flush=True)

        # 當日 production bar(訊號狀態用這個,不用 sidecar)
        # ⚠ store.read_recent 回傳「由新到舊」,本模組假設「由舊到新」——
        #   不反轉會靜默算出反向統計(2026-08-24 上線時被覆蓋率護欄擋下)。
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
            # 個股統計走 sidecar(不足時傳空 → INSUFFICIENT_HISTORY);
            # 族群訊號走 production 當日 bar
            hmax = hist[c][-1]["data_date"] if hist[c] else None
            scored.append(osc.score_one(
                c, hist[c], by_sector.get(sec, {}),
                sec_rank.get(sec), stage_by_code.get(c),
                signal_days=signal_days.get(c, set()),
                audit={"score_date": d.isoformat(),
                       "history_max_date": hmax,
                       "sidecar_build_id": ohist.build_id()}))

        try:
            n = osnap.write_snapshot(d, scored)
        except osnap.SnapshotMutationRefused as exc:
            # 同日重跑但輸入已變 —— 這是設計上的拒絕,不是程式錯誤。
            # 當天的 live 樣本必須保持當時看到的狀態。
            print(f"[opportunity] ✋ {exc}", flush=True)
            return 3
        except osnap.RetroactiveWriteRefused as exc:
            print(f"[opportunity] ✋ {exc}", flush=True)
            return 3
        b = osnap.backfill()

        tiers: dict[str, int] = {}
        for r in scored:
            tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
        top = sum(1 for r in scored if r["signal_in_top_sector"])
        per_stock = sum(1 for r in scored if r.get("stock_level_available"))
        print(f"[opportunity] {d} 快照 {n} 列 / 回填 {b} 列 / 族群 Top10% {top} 檔 / "
              f"個股層可用 {per_stock}/{len(scored)} / 分層 {tiers}", flush=True)
        if n == 0:
            # append-only 語意下,0 列代表「同日重跑且結果完全相同」= 正常的
            # idempotent no-op,不是失敗。
            print("[opportunity] (同日重跑,結果與原始快照一致 → no-op)", flush=True)
        return 0
    except Exception as e:
        print(f"[opportunity] 失敗: {type(e).__name__}: {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
