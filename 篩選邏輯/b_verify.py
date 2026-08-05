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

# ── 加分制(Confidence Score)── 鐵律3:法人是 confidence 的一項加分,不是進場觸發 ──
# confidence = 盤中行為分(b_discover 四判準,主) + 法人加分(副,非閘)
# 法人賣超只扣分、不歸零行為分;法人資料未到 → 留驗(PENDING),不判 FAIL。
# 常數集中在此,調門檻改這裡即可。
CRIT_WEIGHT = {"持續性": 20, "下殺承接": 25, "相對族群強度": 20, "量增價穩": 15}  # 行為 max 80
INST_FULL_NET = 500      # 法人淨買超達此張數 → 給滿額加分
INST_MAX_BONUS = 20      # 法人買超最高加分(confidence 的一項,非觸發)
INST_SELL_PENALTY = 10   # 法人賣超扣分上限(不歸零行為分)
PASS_SCORE = 50          # confidence ≥ 此值 → 進明日候選池


def _inst_bonus(net) -> float:
    """法人淨買超 → confidence 加分(非閘)。買超 +0..MAX、賣超 -0..PENALTY、無資料 0。"""
    if net is None:
        return 0.0
    if net >= 0:
        return round(min(INST_MAX_BONUS, INST_MAX_BONUS * net / INST_FULL_NET), 1)
    return round(-min(INST_SELL_PENALTY, INST_SELL_PENALTY * abs(net) / INST_FULL_NET), 1)


def confidence(passed_criteria, net):
    """純函式(可單元測試):回 (score, behavior, inst_bonus)。"""
    behavior = sum(CRIT_WEIGHT.get(k, 0) for k in (passed_criteria or []))
    ib = _inst_bonus(net)
    return round(behavior + ib, 1), behavior, ib


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
        passed_crit = detail.get("passed", [])
        score, behavior, ib = confidence(passed_crit, net)

        if net is None:
            verdict = "PENDING"
            reason = (f"法人資料未到 → 留驗;盤中行為分 {behavior}"
                      f"(判準 {'、'.join(passed_crit) or '—'}),明日補驗法人")
            pending.append({"code": code, "hits": row.get("hits"),
                            "confidence": score, "behavior": behavior, "reason": reason})
        elif score >= PASS_SCORE:
            verdict = "PASS"
            reason = (f"confidence {score} ≥ {PASS_SCORE} = 行為 {behavior} + 法人 {ib:+g}"
                      f"(淨 {net:+g} 張);判準 {'、'.join(passed_crit) or '—'}")
            passed.append({
                "code": code, "hits": row.get("hits"), "inst_net": net,
                "confidence": score, "behavior": behavior, "inst_bonus": ib,
                "passed_criteria": passed_crit, "source": "B鏈發現", "reason": reason,
            })
        else:
            verdict = "FAIL"
            reason = (f"confidence {score} < {PASS_SCORE} = 行為 {behavior} + 法人 {ib:+g}"
                      f"(淨 {net:+g} 張);行為不足以撐過門檻")
            failed.append({"code": code, "hits": row.get("hits"), "inst_net": net,
                           "confidence": score, "behavior": behavior, "reason": reason})

        rows.append({
            "data_date": d.isoformat(), "code": code,
            "verdict": verdict, "inst_net": net, "reason": reason,
            "verified_at": now,
        })

    store.upsert_intraday(TABLE, PLUGIN, rows, db_path)

    return {
        "chain": "B", "data_date": d.isoformat(), "verified_at": now,
        "purpose": (f"B鏈加分制驗證:標記 {len(disc)} 檔 → confidence≥{PASS_SCORE} 通過 {len(passed)} 檔"
                    f"、留驗 {len(pending)} 檔、淘汰 {len(failed)} 檔;法人為加分非觸發"),
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
