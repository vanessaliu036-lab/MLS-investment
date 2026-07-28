"""
merge_pool.py — A 鏈 / B 鏈 盤後匯流

這是兩條鏈唯一的交會點。其餘時候 A 與 B 互不相干:
  - B 鏈不讀 A 鏈的候選池
  - A 鏈不讀 B 鏈的任何表
  - 各自有專屬的表、專屬的 owner、專屬的插件

匯流公式:
    明日候選池 = A鏈寬篩結果 ∪ B鏈驗證通過的

同一檔兩邊都有 → 標「雙鏈確認」。
這種通常最強:既有昨日的結構優勢,今天盤中又實際啟動了。

執行時機:盤後,screen_post.build() 與 b_verify.verify() 都跑完之後。
任一條鏈掛掉,另一條的結果照樣併入 —— 這是失敗隔離。
"""

from __future__ import annotations

import datetime as _dt
import json

import store
from envelope import run_plugin
from phase import today_tw

PLUGIN = "screen_post"        # 寫 candidate_pool 的 owner 仍是 screen_post
POOL_TABLE = "candidate_pool"


def merge(db_path: str = "mls.db", data_date: _dt.date | None = None) -> dict:
    d = data_date or today_tw()

    # A 鏈:已由 screen_post.build() 寫入 candidate_pool
    a_env = run_plugin("A鏈候選池",
                       lambda: store.read_date(POOL_TABLE, d, db_path))
    a_pool = a_env.get({}) or {}

    # B 鏈:驗證通過的新血
    def _b():
        import b_verify
        return b_verify.verify(db_path, d)
    b_env = run_plugin("B鏈驗證", _b)
    b_res = b_env.get({}) or {}
    b_passed = {x["code"]: x for x in (b_res.get("passed") or [])}

    both, a_only, b_only = [], [], []
    rows = []
    now = _dt.datetime.now().isoformat(timespec="seconds")

    for code, row in a_pool.items():
        payload = {}
        if row.get("payload"):
            try:
                payload = json.loads(row["payload"])
            except Exception:
                pass
        if code in b_passed:
            payload["source"] = "雙鏈確認"
            payload["b_criteria"] = b_passed[code].get("passed_criteria")
            payload["b_inst_net"] = b_passed[code].get("inst_net")
            both.append(code)
        else:
            payload["source"] = "A鏈"
            a_only.append(code)
        rows.append({
            "data_date": d.isoformat(), "code": code,
            "rank": row.get("rank"), "score": row.get("score"),
            "track": row.get("track"), "trigger_price": row.get("trigger_price"),
            "entry_rule": row.get("entry_rule"),
            "payload": json.dumps(payload, ensure_ascii=False),
            "generated_at": now,
        })

    # B 鏈獨有的新血
    for code, x in b_passed.items():
        if code in a_pool:
            continue
        b_only.append(code)
        payload = {
            "code": code, "source": "B鏈新血",
            "score": None, "track": "觀察",
            "entry_rule": "盤中發現且法人確認,明日等盤中訊號再進",
            "b_criteria": x.get("passed_criteria"),
            "b_inst_net": x.get("inst_net"),
            "reasons": [f"B鏈{x.get('hits')}項判準通過", x.get("reason", "")],
            "missing": [],
        }
        rows.append({
            "data_date": d.isoformat(), "code": code,
            "rank": 999, "score": None, "track": "觀察",
            "trigger_price": None,
            "entry_rule": payload["entry_rule"],
            "payload": json.dumps(payload, ensure_ascii=False),
            "generated_at": now,
        })

    store.upsert_intraday(POOL_TABLE, PLUGIN, rows, db_path)

    degraded = []
    if not a_env.ok:
        degraded.append(f"A鏈({a_env.reason[:60]})")
    if not b_env.ok:
        degraded.append(f"B鏈({b_env.reason[:60]})")

    return {
        "data_date": d.isoformat(), "merged_at": now,
        "purpose": (f"明日候選池 {len(rows)} 檔 = A鏈 {len(a_pool)} "
                    f"+ B鏈新血 {len(b_only)},其中雙鏈確認 {len(both)} 檔"),
        "degraded": degraded,       # 任一鏈掛掉,另一鏈照樣併入
        "total": len(rows),
        "both_chains": both,
        "a_only": a_only,
        "b_only": b_only,
    }
