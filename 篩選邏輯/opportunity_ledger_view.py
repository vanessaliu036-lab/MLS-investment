"""Opportunity Ledger —— 呈現層,唯讀。

⚠ 這支檔案不做任何計分/分層/證據判定。所有 tier、evidence、六項指標
一律直接讀 `opportunity_snapshot`(由 opportunity_score.py + opportunity_
snapshot.py 算好並寫入)的既有欄位,原樣顯示或原樣重新命名文案,
**不重算、不套 threshold、不重新排序證據強度**。

背景(2026-08-25 Vanessa 定案的呈現層规則):
  1. Technical State 目前沒有 production-valid 資料 → 整區隱藏,不留空白
     placeholder(空白會被誤讀成「資料漏抓」)。
  2. PRIMARY / HIGH_POTENTIAL 卡片必須明講「Operational Tier,不是
     evidence tier,不是買進建議」,不能只靠顏色/線條讓人自己猜。
  3. 六項數字一律加上「Historical」前綴 + 「conditional n=<n> ·
     DESCRIPTIVE ONLY」——不得叫 Probability / Expected(那是 forward
     predictive 模型才能用的詞),因為 stock-level evidence 尚未 validated。
  4. 唯一資料來源是 production `opportunity_snapshot`,本檔只做 SELECT。
  5. 排序固定 PRIMARY → HIGH_POTENTIAL → WATCH → AVOID,同層內用 code
     升冪——不得用 historical PF / hit rate 這些尚未驗證的個股統計再排序。
  6. Live Evidence 只讀 actual_hit_t10/t15 IS NOT NULL 的實際成熟樣本數,
     不得顯示 "model accuracy improved" / "validated stock pick" /
     "confidence score" 這類字樣。
  7. stock-level 數字缺乏(sector 未觸發,或觸發但樣本 < MIN_CONDITIONAL_N)
     時顯示文字說明,不顯示 0% / PF 0 這種會被誤讀成「真的是零」的數字。
"""
from __future__ import annotations
import sqlite3
from typing import Optional

TIER_ORDER = ["PRIMARY", "HIGH_POTENTIAL", "WATCH", "AVOID"]

TIER_LABEL = {
    "PRIMARY": "Primary",
    "HIGH_POTENTIAL": "High Potential",
    "AVOID": "Avoid",
    "WATCH": "Watch",
}

# 只有這兩層有「容易被誤讀成推薦」的風險 —— 個股層仍是 DESCRIPTIVE ONLY,
# WATCH(訊號未觸發)與 AVOID(已排除)本身語意已經夠清楚,不需要這行。
OPERATIONAL_TIER_NOTE = {
    "PRIMARY": "Operational Tier — 依 frozen signal 分層,不是 evidence tier,不是買進建議",
    "HIGH_POTENTIAL": "Operational Tier — 依 frozen signal 分層,不是 evidence tier,不是買進建議",
}

# 六項指標欄位名 → (顯示文案, DB 欄位)。一律加 Historical 前綴,
# 直到 stock-level forward predictive model 真正 validated 才能改叫
# Probability / Expected。
METRIC_FIELDS = [
    ("Historical +3% Hit Rate", "p_hit_3pct", "%"),
    ("Historical Avg Upside", "expected_upside", "%"),
    ("Historical Avg Downside / MAE", "expected_downside", "%"),
    ("Historical Net Win Rate", "net_positive_rate", "%"),
    ("Historical PF", "profit_factor", ""),
    ("Historical Net Expectancy", "net_expectancy", "%"),
]


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v:+.1f}%" if v is not None else "—"


def _fmt_plain_pct(v: Optional[float]) -> str:
    return f"{v:.1f}%" if v is not None else "—"


def _fmt_ratio(v: Optional[float]) -> str:
    return f"{v:.2f}" if v is not None else "—"


def _metric_value_str(field: str, unit: str, v: Optional[float]) -> str:
    if v is None:
        return "—"
    if field == "profit_factor":
        return _fmt_ratio(v)
    if field in ("expected_upside",):
        return _fmt_plain_pct(v)
    if field in ("net_positive_rate",):
        return _fmt_plain_pct(v)
    if field in ("expected_downside",):
        return _fmt_pct(v)
    if field == "p_hit_3pct":
        return _fmt_plain_pct(v)
    if field == "net_expectancy":
        return _fmt_pct(v)
    return str(v)


def _stock_level_state(row: dict) -> str:
    """二選一,純讀 `stock_level_available` 判斷要顯示數字還是說明文字。

    ⚠ 不能拿 `sector_opportunity`(今天這檔是否在族群 Top10%)當閘門——
    那是「今天」的旗標;conditional 統計(`p_hit_3pct` 等六項)是用「過去
    一年內任何一天訊號曾觸發」的樣本算的,兩者時間尺度不同。真實資料裡
    有 sector_opportunity=0(今天沒觸發)但 stock_level_available=1、
    n=62 的例子(過去觸發夠多次),用 sector_opportunity 當閘門會把這種
    合法的個股數字誤判成「不適用」——這是本檔第一版的 bug,不是資料問題。
    """
    if row.get("p_hit_3pct") is None or not row.get("stock_level_available"):
        return "insufficient"
    return "available"


def _build_card(row: dict) -> dict:
    tier = row["tier"]
    state = _stock_level_state(row)

    metrics = None
    insufficient_note = None
    if state == "available":
        metrics = [
            {"label": label, "value": _metric_value_str(field, unit, row.get(field))}
            for label, field, unit in METRIC_FIELDS
        ]
    else:  # insufficient
        n = row.get("stats_sample_n") or 0
        insufficient_note = f"Stock-level history insufficient(conditional n={n})"

    return {
        "code": row["code"],
        "sector_id": row.get("sector_id"),
        "tier": tier,
        "tier_label": TIER_LABEL.get(tier, tier),
        "operational_note": OPERATIONAL_TIER_NOTE.get(tier),
        "sector_level_evidence": row.get("sector_level_evidence"),
        "stock_level_evidence": row.get("stock_level_evidence"),
        "sector_opportunity": bool(row.get("sector_opportunity")),
        "stock_level_state": state,
        "metrics": metrics,
        "stock_level_caveat": (
            f"conditional n={row.get('stats_sample_n')} · DESCRIPTIVE ONLY"
            if state == "available" else None
        ),
        "insufficient_note": insufficient_note,
        "tier_reasons": (row.get("tier_reasons") or "").split(" / "),
        "raw": row,   # 保留原始列,供比對測試用,不進正式渲染
    }


def _live_evidence(conn: sqlite3.Connection, data_date: str) -> dict:
    row = conn.execute(
        "SELECT MIN(data_date) FROM opportunity_snapshot").fetchone()
    live_since = row[0] if row else None
    fs = conn.execute(
        "SELECT frozen_signal_name, frozen_signal_version FROM opportunity_snapshot "
        "WHERE data_date=? LIMIT 1", (data_date,)).fetchone()

    out = {"live_since": live_since,
           "frozen_signal_name": fs[0] if fs else None,
           "frozen_signal_version": fs[1] if fs else None,
           "horizons": {}}
    for h in (10, 15):
        n = conn.execute(
            f"SELECT COUNT(*) FROM opportunity_snapshot WHERE actual_hit_t{h} IS NOT NULL"
        ).fetchone()[0]
        if n == 0:
            status = "NOT YET AVAILABLE(尚無成熟樣本)"
        elif n < 100:
            status = "DESCRIPTIVE ONLY"
        else:
            status = "CONFIRMATORY"
        out["horizons"][h] = {"n": n, "status": status}
    return out


def build_ledger_context(db_path: str, data_date: Optional[str] = None) -> dict:
    """唯讀組裝渲染用的 context。不重算任何 tier/evidence/score。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if data_date is None:
            r = conn.execute("SELECT MAX(data_date) FROM opportunity_snapshot").fetchone()
            data_date = r[0] if r else None
        if data_date is None:
            return {"data_date": None, "tiers": [], "live_evidence": None, "total_rows": 0}

        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM opportunity_snapshot WHERE data_date=? ORDER BY code",
            (data_date,))]
        by_tier = {t: [] for t in TIER_ORDER}
        for row in rows:
            by_tier.setdefault(row["tier"], []).append(_build_card(row))

        tiers = [{"tier": t, "label": TIER_LABEL.get(t, t), "cards": by_tier.get(t, [])}
                 for t in TIER_ORDER]

        return {
            "data_date": data_date,
            "tiers": tiers,
            "live_evidence": _live_evidence(conn, data_date),
            "total_rows": len(rows),
        }
    finally:
        conn.close()
