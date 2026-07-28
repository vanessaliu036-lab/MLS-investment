"""
b_verify.py — B 鏈:盤後法人驗證

⚠ B 鏈,與 A 鏈完全獨立。這支只讀 b_discovery 和 inst_flow,
   只寫 b_verified。不碰 A 鏈的 candidate_pool。
   這支爆掉,A 鏈的盤後寬篩照常產出候選池。

任務:把 13:20 標記的股票,用今日法人數字驗一次。

為什麼要驗:
  盤中發現的東西沒有法人蓋章,你不能當天就追。
  盤中的 aflow 是主動買賣推估,不是法人買賣超(鐵律3)。
  正確流程是:盤中標記 → 收盤後驗證是不是真的有人買 → 明天才進候選池。

三態判定:
  PASS     法人今日買超 → 進明日候選池
  FAIL     法人今日賣超 → 淘汰。盤中那個異動是散戶或當沖,不是資金進駐
  NO_DATA  法人資料沒到 → 留著,明天再驗。NO_DATA 絕不等於 FAIL

執行時間:13:31 之後,官方法人資料到位即可跑。
"""

from __future__ import annotations

import datetime as _dt
import json

import store
from envelope import run_all, persist_status, missing_labels
from phase import Phase, today_tw

PLUGIN = "b_verify"
TABLE = "b_verified"

PASS_MIN_NET = 0        # 法人淨買超門檻(張)。0 = 只要是買超就算。


def verify(db_path: str = "mls.db", data_date: _dt.date | None = None) -> dict:
    d = data_date or today_tw()

    envs = run_all({
        "discovery": lambda: store.read_date("b_discovery", d, db_path),
        "inst": lambda: store.read_date("inst_flow", d, db_path),
    }, phase=Phase.POST)
    persist_status(envs, db_path)

    disc = envs["discovery"].get({}) or {}
    inst = envs["inst"].get({}) or {}

    if not disc:
        return {
            "chain": "B", "data_date": d.isoformat(),
            "purpose": "B鏈驗證 — 今日無標記標的",
            "degraded": missing_labels(envs),
            "passed": [], "failed": [], "pending": [],
        }

    passed, failed, pending = [], [], []
    rows = []
    now = _dt.datetime.now().isoformat(timespec="seconds")

    for code, row in disc.items():
        detail = {}
        if row.get("detail"):
            try:
                detail = json.loads(row["detail"])
            except Exception:
                pass

        rec = inst.get(code)
        net = (rec or {}).get("total_net")

        if net is None:
            verdict, reason = "NO_DATA", "法人資料尚未到位,明日再驗"
            pending.append({"code": code, "hits": row.get("hits"), "reason": reason})
        elif net > PASS_MIN_NET:
            verdict, reason = "PASS", f"法人買超 {net} 張,盤中異動獲確認"
            passed.append({
                "code": code, "hits": row.get("hits"), "inst_net": net,
                "passed_criteria": detail.get("passed", []),
                "source": "B鏈發現", "reason": reason,
            })
        else:
            verdict, reason = "FAIL", f"法人賣超 {abs(net)} 張,盤中異動非資金進駐"
            failed.append({"code": code, "hits": row.get("hits"),
                           "inst_net": net, "reason": reason})

        rows.append({
            "data_date": d.isoformat(), "code": code,
            "verdict": verdict, "inst_net": net, "reason": reason,
            "verified_at": now,
        })

    store.upsert_intraday(TABLE, PLUGIN, rows, db_path)

    return {
        "chain": "B", "data_date": d.isoformat(), "verified_at": now,
        "purpose": (f"B鏈驗證:標記 {len(disc)} 檔 → 通過 {len(passed)} 檔,"
                    f"將併入明日候選池"),
        "degraded": missing_labels(envs),
        "marked": len(disc),
        "passed": passed, "failed": failed, "pending": pending,
    }


def load_passed(data_date: _dt.date | None = None,
                db_path: str = "mls.db") -> list[str]:
    """給匯流用:今日通過驗證的代號。這是 B 鏈唯一對外的出口。"""
    d = (data_date or today_tw()).isoformat()
    with store.conn(db_path) as c:
        rows = c.execute(
            "SELECT code FROM b_verified WHERE data_date=? AND verdict='PASS'", (d,)
        ).fetchall()
    return [r["code"] for r in rows]
