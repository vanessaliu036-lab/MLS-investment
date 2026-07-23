"""盤後蓋章歷史回溯的分類規則與資料整形。"""


GROUP_RANK = {"排除": 0, "觀察": 1, "可操作": 2}


def classify_eod(row):
    score = row.get("score") or 0
    grade = row.get("grade") or "Watch"
    hard = row.get("hard_risk") or row.get("scoring_hard_risk")
    if hard or grade == "Hold" or score < 50:
        group = "排除"
        sub = "硬風險/分數不足"
    elif grade == "Ready" and row.get("above_ma20"):
        group = "可操作"
        sub = "條件齊全"
    else:
        group = "觀察"
        sub = "MA20待確認" if not row.get("above_ma20") else "條件待確認"
    return {"group": group, "subgroup": sub, "rank": GROUP_RANK[group]}


def classify_trend(rows):
    if len(rows) < 2:
        return "分類穩定"
    previous = GROUP_RANK.get(rows[-2].get("group"), 0)
    current = GROUP_RANK.get(rows[-1].get("group"), 0)
    if current > previous:
        return "分類爬升"
    if current < previous:
        return "分類下降"
    return "分類穩定"
