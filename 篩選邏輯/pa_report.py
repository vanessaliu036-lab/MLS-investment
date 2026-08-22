"""Pre-Activation v0.1 — Live Observation 讀出。

只讀 pa_snapshot,不重算、不調規則。固定追四件事:
  1. 各 stage 的 T+3 / T+5 / T+7 淨報酬
  2. 各 stage 的 Profit Factor、正報酬率、Avg Win / Avg Loss
  3. ARMED → TRIGGER 轉換後的表現
  4. EXTENDED 被禁追後,是否真的明顯低於其他 stage

⚠ 重點不是「TRIGGER 有沒有漲」,而是「哪一個 stage 最有賺錢價值,
   以及從哪個 stage 進場最早、但又不會太早」。
"""
from __future__ import annotations
import store

STAGES = ("EARLY", "ARMED", "TRIGGER", "EXTENDED", "—")
MIN_N = 20        # 少於此筆數只列出、不下結論


def _stats(rows: list[tuple]) -> dict:
    vals = [r for r in rows if r is not None]
    if not vals:
        return {}
    win = [v for v in vals if v > 0]
    loss = [v for v in vals if v <= 0]
    return {
        "n": len(vals),
        "net_expectancy": round(sum(vals) / len(vals), 3),
        "profit_factor": (round(sum(win) / -sum(loss), 3) if loss and sum(loss) < 0
                          else None),
        "positive_rate": round(len(win) / len(vals) * 100, 1),
        "avg_win": round(sum(win) / len(win), 3) if win else None,
        "avg_loss": round(sum(loss) / len(loss), 3) if loss else None,
        "max_loss": round(min(vals), 2),
        "enough": len(vals) >= MIN_N,
    }


def by_stage(db_path: str = "mls.db", horizons=(3, 5, 7)) -> dict:
    out = {}
    with store.conn(db_path) as c:
        for st in STAGES:
            rec = {}
            for h in horizons:
                rows = [r[0] for r in c.execute(
                    f"SELECT net_t{h} FROM pa_snapshot WHERE stage=? AND net_t{h} IS NOT NULL",
                    (st,)).fetchall()]
                s = _stats(rows)
                if s:
                    rec[f"T+{h}"] = s
            pend = c.execute(
                "SELECT COUNT(*) FROM pa_snapshot WHERE stage=? AND net_t7 IS NULL",
                (st,)).fetchone()[0]
            rec["pending"] = pend
            out[st] = rec
    return out


def armed_to_trigger(db_path: str = "mls.db", horizon: int = 5) -> dict:
    """ARMED 之後真的升到 TRIGGER 的那批,對照「一直停在 ARMED」的那批。"""
    with store.conn(db_path) as c:
        rows = c.execute(
            "SELECT a.code, a.data_date, "
            f"       (SELECT b.stage FROM pa_snapshot b WHERE b.code=a.code "
            "         AND b.data_date>a.data_date ORDER BY b.data_date LIMIT 1) nxt, "
            f"       a.net_t{horizon} "
            "FROM pa_snapshot a WHERE a.stage='ARMED'").fetchall()
    up = [r[3] for r in rows if r[2] == "TRIGGER" and r[3] is not None]
    stay = [r[3] for r in rows if r[2] != "TRIGGER" and r[3] is not None]
    return {"armed_to_trigger": _stats(up), "armed_stayed": _stats(stay)}


def summary_text(db_path: str = "mls.db") -> str:
    d = by_stage(db_path)
    lines = ["Pre-Activation v0.1 — Live Observation(8/24 起)", ""]
    for st in STAGES:
        rec = d.get(st) or {}
        t5 = rec.get("T+5") or {}
        if not t5:
            lines.append(f"{st:<10} 尚無到期樣本(待回填 {rec.get('pending', 0)} 筆)")
            continue
        tag = "" if t5.get("enough") else "  ⚠樣本不足,僅列出不下結論"
        lines.append(
            f"{st:<10} T+5 n={t5['n']:<4} 淨期望 {t5['net_expectancy']:+.3f}% "
            f"PF {t5['profit_factor'] if t5['profit_factor'] is not None else '—'} "
            f"正報酬 {t5['positive_rate']}% "
            f"賺{t5['avg_win']}/賠{t5['avg_loss']}{tag}")
    return "\n".join(lines)
