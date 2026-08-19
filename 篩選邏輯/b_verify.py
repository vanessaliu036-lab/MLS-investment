"""
b_verify.py — B 鏈:盤後收盤驗證(2026-08-19 改版,取消淘汰權)

⚠ B 鏈,與 A 鏈完全獨立。這支只讀 b_discovery 和 inst_flow,
   只寫 b_verified。不碰 A 鏈的 candidate_pool。
   這支爆掉,A 鏈的盤後寬篩照常產出候選池。

任務:把 13:20 標記的股票,用今日法人數字驗一次「盤中訊號有沒有被收盤確認」。

鐵律(2026-08-19 定案,取代舊 PASS/FAIL 淘汰制):
  B 鏈只能改 verification_status,不能改 eligibility_status。
  confidence 是「收盤確認程度」,不是「股票該不該活」的判準。
  舊版 confidence<50 直接 FAIL 淘汰,曾把「今天沒被驗證確認」錯當成「明天不會漲」,
  是複盤查到的反指標來源之一(2026-08-19)——盤中訊號沒被今天的法人數字驗到,
  常常只是資金明天才進場,不是訊號是假的。改版後這批股照樣進明日候選池,
  只是帶著 verification_status=UNCONFIRMED 讓下游知道「還沒驗到」,不是「已判死」。

四態判定(confidence 分數只分驗證程度,不分生死):
  CONFIRMED    confidence >= 70          → ✅ 收盤確認
  PARTIAL      50 <= confidence < 70     → 🟡 部分確認
  UNCONFIRMED  confidence < 50           → ⚪ 未確認(不淘汰,照樣進池)
  NO_DATA      法人資料沒到              → 法人資料未到,留待補驗

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

# ── 驗證程度門檻(只分 verification_status,不分 eligibility) ──
CONFIRM_SCORE = 70       # confidence >= 此值 → 收盤確認
PARTIAL_SCORE = 50       # confidence >= 此值(< CONFIRM_SCORE) → 部分確認

VERDICT_CONFIRMED = "CONFIRMED"
VERDICT_PARTIAL = "PARTIAL"
VERDICT_UNCONFIRMED = "UNCONFIRMED"
VERDICT_NO_DATA = "NO_DATA"

VERDICT_LABEL = {
    VERDICT_CONFIRMED: "✅ 收盤確認",
    VERDICT_PARTIAL: "🟡 部分確認",
    VERDICT_UNCONFIRMED: "⚪ 未確認",
    VERDICT_NO_DATA: "⚪ 法人資料未到",
}


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
            "verified": [], "confirmed": [], "partial": [], "unconfirmed": [], "no_data": [],
        }

    verified: list[dict] = []
    confirmed, partial, unconfirmed, no_data = [], [], [], []
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
            verdict = VERDICT_NO_DATA
            reason = (f"法人資料未到 → 留驗;盤中行為分 {behavior}"
                      f"(判準 {'、'.join(passed_crit) or '—'}),明日補驗法人")
            bucket = no_data
        elif score >= CONFIRM_SCORE:
            verdict = VERDICT_CONFIRMED
            reason = (f"confidence {score} ≥ {CONFIRM_SCORE} = 行為 {behavior} + 法人 {ib:+g}"
                      f"(淨 {net:+g} 張);判準 {'、'.join(passed_crit) or '—'}")
            bucket = confirmed
        elif score >= PARTIAL_SCORE:
            verdict = VERDICT_PARTIAL
            reason = (f"confidence {score} 介於 {PARTIAL_SCORE}~{CONFIRM_SCORE} = 行為 {behavior}"
                      f" + 法人 {ib:+g}(淨 {net:+g} 張);部分確認,非淘汰判準")
            bucket = partial
        else:
            verdict = VERDICT_UNCONFIRMED
            reason = (f"confidence {score} < {PARTIAL_SCORE} = 行為 {behavior} + 法人 {ib:+g}"
                      f"(淨 {net:+g} 張);今天沒被收盤驗證確認 ≠ 明天不會漲,不淘汰、照樣進池")
            bucket = unconfirmed

        item = {
            "code": code, "hits": row.get("hits"), "inst_net": net,
            "confidence": score, "behavior": behavior, "inst_bonus": ib,
            "passed_criteria": passed_crit, "source": "B鏈發現",
            "verification_status": verdict,
            "verification_label": VERDICT_LABEL[verdict],
            "reason": reason,
        }
        bucket.append(item)
        verified.append(item)

        rows.append({
            "data_date": d.isoformat(), "code": code,
            "verdict": verdict, "inst_net": net, "reason": reason,
            "verified_at": now,
        })

    store.upsert_intraday(TABLE, PLUGIN, rows, db_path)

    return {
        "chain": "B", "data_date": d.isoformat(), "verified_at": now,
        "purpose": (f"B鏈收盤驗證(不淘汰):標記 {len(disc)} 檔 → "
                    f"✅確認 {len(confirmed)}、🟡部分確認 {len(partial)}、"
                    f"⚪未確認 {len(unconfirmed)}、待補資料 {len(no_data)};"
                    f"全部照樣進明日候選池"),
        "degraded": missing_labels(envs),
        "marked": len(disc),
        "verified": verified,
        "confirmed": confirmed, "partial": partial,
        "unconfirmed": unconfirmed, "no_data": no_data,
    }


def load_verified(data_date: _dt.date | None = None,
                  db_path: str = "mls.db") -> list[str]:
    """給匯流用:今日所有被 B 鏈標記並驗過的代號(不論 verification_status)。
    B 鏈不再過濾淘汰,誰進 candidate_pool 由 merge_pool 決定,這支只回全量。"""
    d = (data_date or today_tw()).isoformat()
    with store.conn(db_path) as c:
        rows = c.execute(
            "SELECT code FROM b_verified WHERE data_date=?", (d,)
        ).fetchall()
    return [r["code"] for r in rows]
