# -*- coding: utf-8 -*-
"""市場資金廣度（Market Breadth）— 51 檔資金流入的「面」而不是「點」。

單日的「資金流入占比」只回答一個問題：現在是多少檔股票在被買，
不是哪一檔可以買。這個模組把它升級成兩件事：

  1. 作戰模式判讀：>70% = Risk On（可積極做多、提高持股比重），
     <30% = Risk Off（資金撤退，保守）。
  2. 真假行情判讀：指數與廣度背離時直接點名。加權指數 +2% 但 51 檔
     只有 13 檔 aflow>0（26%）→ 指數是被台積電／鴻海／廣達拉起來的，
     典型窄幅行情（Narrow Breadth），不能當成全面性上攻。
  3. 時間序列：單日 71% 的價值遠低於「連 3 日一天比一天多股票流入」。
     每輪盤中把廣度落地 SQLite，累積日線序列與日內曲線。

與 review_rules 同慣例：不打任何外部 API、不寫 mls.db，
資料落在獨立的 intraday_eod.db；缺當日資料時回退最近一日並標明資料日。
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "intraday_eod.db"   # 測試可覆寫
TW_TZ = ZoneInfo("Asia/Taipei")

# ------------------ 作戰模式門檻（要調靈敏度只改這裡） ------------------
LEVELS = [
    # (下限 %, 代號, 標題, 白話操作建議)
    (70, "risk_on",  "Risk On",  "可積極操作，可提高持股比重"),
    (55, "lean_long", "偏多",    "可正常做多，先挑資金連續流入的族群"),
    (45, "neutral",  "中性",     "選股不選市，控制單筆部位"),
    (30, "lean_short", "偏空",   "減碼、只留手上最強的，不新增部位"),
    (0,  "risk_off", "Risk Off", "資金撤退，保守觀望為主"),
]

# 背離門檻：指數漲跌幅（%）與流入占比（%）
DIVERGE_INDEX_UP = 1.0      # 指數漲這麼多以上才談「是不是假行情」
DIVERGE_INDEX_DOWN = -1.0
NARROW_RATIO = 40.0         # 指數漲、流入占比低於此 → 窄幅行情
BROAD_RATIO = 60.0          # 指數漲、流入占比高於此 → 全面性上攻
RESILIENT_RATIO = 55.0      # 指數跌但流入占比高於此 → 資金留守
# ----------------------------------------------------------------------

# 盤中每 5 分鐘落一點日內曲線，避免每輪都寫一筆把表灌爆。
TICK_BUCKET_MIN = 5
SESSION = ((8, 55), (13, 35))
# 服務剛重啟時 Shioaji buffer 可能只有個位數回報，1 檔流入就是 100%。
# 樣本不足一律不落地，也不准用小樣本蓋掉當日已記錄的大樣本。
MIN_SAMPLE = 30


def _now():
    return datetime.now(TW_TZ)


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS breadth_daily(
        trade_date  TEXT PRIMARY KEY,
        total       INTEGER,
        in_count    INTEGER,
        ratio_pct   REAL,
        index_pct   REAL,
        level       TEXT,
        state       TEXT,
        updated_at  TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS breadth_ticks(
        trade_date  TEXT NOT NULL,
        hhmm        TEXT NOT NULL,
        ratio_pct   REAL,
        in_count    INTEGER,
        total       INTEGER,
        index_pct   REAL,
        PRIMARY KEY (trade_date, hhmm))""")
    return conn


def level_of(ratio_pct):
    """流入占比 → (代號, 標題, 操作建議)。"""
    for floor, code, title, advice in LEVELS:
        if ratio_pct >= floor:
            return code, title, advice
    return LEVELS[-1][1], LEVELS[-1][2], LEVELS[-1][3]


def diagnose(ratio_pct, index_pct):
    """指數 vs 廣度背離判讀 → (狀態代號, 標題, 白話)。

    這層專門回答「是不是假行情」：指數漲不算數，要看有多少檔在漲。"""
    if index_pct is None:
        return "unknown", "指數未知", "加權指數暫無資料，僅以資金廣度判讀"
    if index_pct >= DIVERGE_INDEX_UP and ratio_pct < NARROW_RATIO:
        return ("narrow", "窄幅行情（Narrow Breadth）",
                f"加權指數 +{index_pct:.2f}%，但 51 檔只有 {ratio_pct:.0f}% 資金流入；"
                "指數上漲、資金沒跟，很可能只是少數權值股把指數拉起來，不宜追價")
    if index_pct >= DIVERGE_INDEX_UP and ratio_pct >= BROAD_RATIO:
        return ("broad", "全面性上攻",
                f"加權指數 +{index_pct:.2f}%，且 {ratio_pct:.0f}% 個股資金流入；"
                "指數與資金同步，行情有基礎")
    if index_pct <= DIVERGE_INDEX_DOWN and ratio_pct >= RESILIENT_RATIO:
        return ("resilient", "指數殺低、資金留守",
                f"加權指數 {index_pct:.2f}%，但仍有 {ratio_pct:.0f}% 個股資金流入；"
                "賣壓集中在權值股，個股不必跟著恐慌")
    if index_pct <= DIVERGE_INDEX_DOWN and ratio_pct < NARROW_RATIO:
        return ("broad_down", "全面性撤退",
                f"加權指數 {index_pct:.2f}%，資金流入只剩 {ratio_pct:.0f}%；"
                "指數與資金同步走弱，不接刀")
    return ("aligned", "指數與資金一致",
            f"加權指數 {index_pct:+.2f}%、資金流入占比 {ratio_pct:.0f}%，兩者沒有明顯背離")


def compute(rows, index_pct=None):
    """從盤中 rows 算今日廣度。rows 需含 aflow（None 視為無資料，不計分母）。"""
    valid = [r for r in rows if r.get("aflow") is not None]
    total = len(valid)
    in_count = sum(1 for r in valid if float(r.get("aflow") or 0) > 0)
    ratio = round(in_count / total * 100, 1) if total else 0.0
    # 服務剛重啟時 buffer 可能只有個位數回報，1 檔流入就是 100%，會誤判
    # Risk On。樣本不足時把占比視為無效（no_data），不落地、不判作戰模式。
    thin = total < MIN_SAMPLE
    code, title, advice = level_of(ratio)
    state, state_title, state_note = diagnose(ratio, index_pct)
    return {
        "total": total,
        "in_count": in_count,
        "out_count": total - in_count,
        "ratio_pct": ratio,
        "index_pct": index_pct,
        "level": code,
        "level_title": title,
        "level_advice": advice,
        "state": state,
        "state_title": state_title,
        "state_note": state_note,
        "thin_sample": thin,
        "no_data": total == 0 or thin,
    }


def record(breadth, now=None):
    """盤中每輪呼叫：更新今日廣度（最後一筆即收盤值），並落 5 分鐘日內曲線。"""
    now = now or _now()
    if breadth.get("no_data"):
        return False
    hm = (now.hour, now.minute)
    if not (SESSION[0] <= hm <= SESSION[1]):
        return False
    day = now.date().isoformat()
    bucket = f"{now.hour:02d}:{now.minute // TICK_BUCKET_MIN * TICK_BUCKET_MIN:02d}"
    with _conn() as conn:
        conn.execute(
            """INSERT INTO breadth_daily
               (trade_date, total, in_count, ratio_pct, index_pct, level, state, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(trade_date) DO UPDATE SET
                 total=excluded.total, in_count=excluded.in_count,
                 ratio_pct=excluded.ratio_pct, index_pct=excluded.index_pct,
                 level=excluded.level, state=excluded.state,
                 updated_at=excluded.updated_at""",
            (day, breadth["total"], breadth["in_count"], breadth["ratio_pct"],
             breadth["index_pct"], breadth["level"], breadth["state"],
             now.isoformat(timespec="seconds")))
        conn.execute(
            """INSERT INTO breadth_ticks (trade_date, hhmm, ratio_pct, in_count, total, index_pct)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(trade_date, hhmm) DO UPDATE SET
                 ratio_pct=excluded.ratio_pct, in_count=excluded.in_count,
                 total=excluded.total, index_pct=excluded.index_pct""",
            (day, bucket, breadth["ratio_pct"], breadth["in_count"],
             breadth["total"], breadth["index_pct"]))
    return True


def history(days=10, before=None):
    """近 N 個交易日的廣度序列（舊→新）。"""
    with _conn() as conn:
        if before:
            rows = conn.execute(
                """SELECT * FROM breadth_daily WHERE trade_date<=?
                   ORDER BY trade_date DESC LIMIT ?""", (before, days)).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM breadth_daily
                   ORDER BY trade_date DESC LIMIT ?""", (days,)).fetchall()
    return [dict(r) for r in reversed(rows)]


def today_curve(day=None):
    """今日日內廣度曲線（5 分鐘一點，舊→新）。"""
    day = day or _now().date().isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT hhmm, ratio_pct, in_count, total, index_pct
               FROM breadth_ticks WHERE trade_date=? ORDER BY hhmm""",
            (day,)).fetchall()
    return [dict(r) for r in rows]


def trend(series):
    """序列 → 擴散判讀。連續幾天更多股票加入資金流入，比單日 71% 有價值。"""
    if len(series) < 2:
        return {"streak": 0, "direction": "首日", "delta": None,
                "avg5": series[-1]["ratio_pct"] if series else None,
                "note": "資金廣度序列剛開始累積，明日起可比較擴散或收縮"}
    streak, direction = 0, None
    for i in range(len(series) - 1, 0, -1):
        d = series[i]["ratio_pct"] - series[i - 1]["ratio_pct"]
        step = "up" if d > 0 else ("down" if d < 0 else "flat")
        if direction is None:
            if step == "flat":
                direction = "flat"
                break
            direction = step
        if step != direction:
            break
        streak += 1
    delta = round(series[-1]["ratio_pct"] - series[-2]["ratio_pct"], 1)
    window = [r["ratio_pct"] for r in series[-5:]]
    avg5 = round(sum(window) / len(window), 1)
    if direction == "up":
        note = (f"資金連 {streak} 日擴散：每天都有更多股票加入資金流入"
                f"（近 5 日均 {avg5}%）")
    elif direction == "down":
        note = (f"資金連 {streak} 日收縮：流入的股票一天比一天少"
                f"（近 5 日均 {avg5}%）")
    else:
        note = f"資金廣度持平（近 5 日均 {avg5}%）"
    return {"streak": streak, "direction": direction or "flat",
            "delta": delta, "avg5": avg5, "note": note}


def api_payload(rows=None, index_pct=None, now=None, days=10):
    """/api/market-breadth 回應：今日廣度 + 日內曲線 + 日線序列 + 擴散判讀。

    rows 有帶就用即時值；沒帶（或今日尚無資料）則回退最近一日並標明資料日。"""
    now = now or _now()
    today = now.date().isoformat()
    today_breadth = compute(rows or [], index_pct) if rows else None
    data_date, stale = today, False

    if today_breadth is None or today_breadth["no_data"]:
        rows_hist = history(1)
        if rows_hist:
            last = rows_hist[-1]
            code, title, advice = level_of(last["ratio_pct"])
            state, state_title, state_note = diagnose(last["ratio_pct"], last["index_pct"])
            today_breadth = {
                "total": last["total"], "in_count": last["in_count"],
                "out_count": (last["total"] or 0) - (last["in_count"] or 0),
                "ratio_pct": last["ratio_pct"], "index_pct": last["index_pct"],
                "level": code, "level_title": title, "level_advice": advice,
                "state": state, "state_title": state_title, "state_note": state_note,
                "no_data": False,
            }
            data_date = last["trade_date"]
            stale = data_date != today
        else:
            today_breadth = today_breadth or compute([], index_pct)

    series = history(days)
    # 今日已算出但還沒落地（例如非交易時段）時，讓序列尾端就是眼前這一筆。
    if series and series[-1]["trade_date"] == data_date:
        series[-1] = {**series[-1], "ratio_pct": today_breadth["ratio_pct"],
                      "in_count": today_breadth["in_count"],
                      "total": today_breadth["total"],
                      "index_pct": today_breadth["index_pct"]}
    elif not today_breadth["no_data"]:
        series = series + [{"trade_date": data_date,
                            "ratio_pct": today_breadth["ratio_pct"],
                            "in_count": today_breadth["in_count"],
                            "total": today_breadth["total"],
                            "index_pct": today_breadth["index_pct"],
                            "level": today_breadth["level"],
                            "state": today_breadth["state"]}]

    payload = dict(today_breadth)
    payload.update({
        "ok": True,
        "trade_date": today,
        "data_date": data_date,
        "stale": stale,
        "series": series,
        "curve": today_curve(data_date),
        "trend": trend(series),
        "thresholds": {"risk_on": 70, "risk_off": 30,
                       "narrow": NARROW_RATIO, "broad": BROAD_RATIO},
        "note": "流入占比＝51 檔中 aflow>0 的比例；這是市場水位，不是個股進場訊號",
    })
    if stale:
        payload["notes"] = [f"今日盤中資料尚未累積，顯示最近一日廣度（{data_date}）"]
    return payload
