#!/usr/bin/env python3
"""funnel.py 每日排程快照(2026-08-27 起)。

⚠ 為什麼需要這支:跟 run_pa_snapshot.py 是同一個病灶(見該檔 docstring)。
   funnel.run() 只從 /api/funnel、/ab/funnel 兩個 HTTP handler 呼叫,
   沒有任何排程程式呼叫它 —— 沒人開那個頁面,funnel_result/funnel_log
   當天就不會寫入。2026-08-27 這天發現:reject_verify.py 靠
   funnel_result.survived=0 撈「被淘汰名單」算誤刪率(FNR),那天沒人查
   /ab/funnel 就等於 FNR pipeline 斷資料 —— 這正是 08-18 之後
   reject_outcome 幾乎沒有新樣本的根因之一。排程獨立跑一次才保證連續。

同一天重跑是安全的:funnel.run() 內部呼叫 store.upsert_intraday(其他
排程腳本也是靠這個語意保證同日重跑不炸表),不會產生重複列。
"""
from __future__ import annotations
import sys

import config
import funnel


def main() -> int:
    try:
        res = funnel.run(list(config.UNIVERSE), dict(config.CODE_GROUP),
                         db_path="mls.db", with_chips=True)
        layers = res.get("layers") or []
        n = res.get("count", 0)
        l4 = next((L for L in layers if L.get("name") == "central_classifier"), None)
        rejected = l4.get("rejected") if l4 else None
        print(f"[funnel_snapshot] {res.get('data_date')} stage={res.get('stage')} "
              f"count={n} L4_rejected={rejected} warnings={res.get('warnings')}", flush=True)
        if not layers:
            print("[funnel_snapshot] ⚠ 一層都沒有,請查 b_snapshot 有沒有在跑", flush=True)
            return 1
        return 0
    except Exception as e:
        print(f"[funnel_snapshot] 失敗: {type(e).__name__}: {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
