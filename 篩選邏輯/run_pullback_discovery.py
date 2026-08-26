#!/usr/bin/env python3
"""Healthy Pullback Entry Framework — 每日排程用(DISCOVERY ONLY，不進 production)。

規格凍結於 winning_model_backtest/FROZEN_HEALTHY_PULLBACK_V1.md(2026-08-26)。
只做兩件事:
  1) 掃 daily_bar 找「真鎖漲停 → 下一交易日」事件，對 b_snapshot 已完整(該日
     盤中已收盤)的案例套凍結公式，append-only 寫進 pullback_discovery。
  2) 幫舊列補 d2_fwd_ret(要等 D+2 收盤價才補得齊)。

同一天重跑安全:write_cases 對已存在且雜湊相同的 (code,d1_date) 直接跳過;
雜湊不同才會拋 SnapshotMutationRefused(代表凍結公式的輸入資料變了，
理論上不該發生，發生了要查，不能默默覆蓋)。
"""
from __future__ import annotations

import sqlite3
import sys

import pullback_discovery as pd
import pullback_discovery_snapshot as snap

DB = "mls.db"
MIN_SLOTS_FOR_FULL_SESSION = 45  # 09:00-13:30 5分鐘格滿檔約54格,45格代表當日已收盤


def _b_snapshot_rows(code: str, d1_date: str, db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT slot,price,volume,net_active FROM b_snapshot "
            "WHERE code=? AND data_date=? ORDER BY slot", (code, d1_date)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def main() -> int:
    events = pd.find_limitup_events(DB)
    cases = []
    skipped_incomplete = 0
    for ev in events:
        slots = _b_snapshot_rows(ev["code"], ev["d1_date"], DB)
        if len(slots) < MIN_SLOTS_FOR_FULL_SESSION:
            skipped_incomplete += 1  # 該日盤中資料還沒收完,下次排程再補
            continue
        case = pd.compute_case(ev, slots)
        if case is not None:
            cases.append(case)

    try:
        written = snap.write_cases(cases, DB)
    except snap.SnapshotMutationRefused as e:
        print(f"[pullback_discovery] 拒寫: {e}", flush=True)
        return 1

    backfilled = snap.backfill_d2(DB)
    print(f"[pullback_discovery] 事件 {len(events)} / 可算 {len(cases)} / "
          f"新寫 {written} / d2回填 {backfilled} / 待收盤跳過 {skipped_incomplete}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
