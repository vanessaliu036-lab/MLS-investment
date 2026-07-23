"""
MLS v4.0 — livermore.py
李佛摩六欄狀態機。每檔每日落在一欄，記錄逐日時間序列，關鍵點★標轉向。
六欄：次級反彈 / 自然反彈 / 上升趨勢 / 下降趨勢 / 自然回檔 / 次級回檔
"""
import db

COLS = ["次級反彈", "自然反彈", "上升趨勢", "下降趨勢", "自然回檔", "次級回檔"]
COLORS = {
    "次級反彈": "#c99a1e", "自然反彈": "#e0662b", "上升趨勢": "#c0342c",
    "下降趨勢": "#1a8a5a", "自然回檔": "#2f6bd0", "次級回檔": "#6a4bb0",
}


def classify(ev):
    """依象限/趨勢/是否站上MA20 判定六欄狀態。"""
    quad = ev["quad"]
    trend = ev["trend"]
    above = ev["above_ma20"]
    if quad == "in_up":
        return "上升趨勢" if (trend == "改善" and above) else "自然反彈"
    if quad == "in_down":
        return "自然回檔"
    if quad == "out_up":
        return "次級反彈"
    if quad == "out_down":
        return "下降趨勢" if not above else "自然回檔"
    return "自然反彈"


def pivot_of(ev, state):
    """關鍵點：突破前波極值的轉向。"""
    if state == "上升趨勢" and ev["close"] >= ev["trigger"] * 0.999:
        return "多方關鍵點"
    if state == "下降趨勢" and not ev["above_ma20"]:
        return "空方關鍵點"
    return None


def record_daily(trade_date, evals):
    """盤後把每檔六欄狀態落地 DB。"""
    for ev in evals:
        state = classify(ev)
        pivot = pivot_of(ev, state)
        db.save_liv(trade_date, ev["code"], state, ev["close"], bool(pivot))


def get_record(code, days=20):
    """回傳單檔逐日六欄紀錄（舊→新）。"""
    rows = db.liv_history(code, days)
    return [{"date": r["trade_date"][5:], "state": r["state"],
             "price": r["price"], "pivot": bool(r["pivot"])} for r in rows]
