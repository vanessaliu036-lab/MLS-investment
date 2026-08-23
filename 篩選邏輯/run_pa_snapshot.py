#!/usr/bin/env python3
"""Pre-Activation 每日快照(排程用,2026-08-24 起)。

⚠ 為什麼需要這支:排程的盤後工作是 collect.py + run_stage2_verify.py,
   兩支都不呼叫 screen_post.build()。快照原本掛在 /api/watchlist 的
   POST 分支,那只有「有人打開頁面」才會跑 —— 沒人開頁面當天就沒有快照,
   live baseline 會斷。排程獨立跑一次才保證連續。

同一天重跑是安全的:write_snapshot 用 INSERT OR REPLACE,
backfill 只補 ret_t7 仍為 NULL 的列。
"""
from __future__ import annotations
import datetime as dt
import sys

import config
import screen_post
import pa_snapshot


def main() -> int:
    try:
        data = screen_post.build(list(config.UNIVERSE))
        items = data.get("items") or []
        for rank, it in enumerate(items, 1):
            it.setdefault("legacy_rank", rank)
        d = dt.date.fromisoformat(data["data_date"])
        n = pa_snapshot.write_snapshot(d, items)
        b = pa_snapshot.backfill()
        missing = [i for i in items if not i.get("pre_activation")]
        print(f"[pa_snapshot] {d} 快照 {n} 列 / 回填 {b} 列 / "
              f"無 stage 欄位 {len(missing)} 檔", flush=True)
        if n == 0:
            print("[pa_snapshot] ⚠ 一列都沒寫入,請查 screen_post 產出", flush=True)
            return 1
        return 0
    except Exception as e:
        print(f"[pa_snapshot] 失敗: {type(e).__name__}: {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
