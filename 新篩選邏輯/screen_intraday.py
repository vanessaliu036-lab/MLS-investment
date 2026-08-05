"""
screen_intraday.py — 盤中嚴判(三態燈號)

定位:真正的淘汰在這裡發生。這是扣扳機前的最後一關,錯了就是真金白銀。
      所以嚴:綠燈必須四個條件全中,少一個就是黃燈續盯,不放行。

漏斗:
    51 檔全集
       ↓ 盤後寬篩(中兩條件才砍)
    15-20 檔候選池 ← 盤中只盯這些,不掃全市場
       ↓ 盤中嚴判(四條件全中才綠燈)
    2-5 檔綠燈 ← 到你預設的進場條件
       ↓ 你按 ATR 紀律扣扳機
    實際持倉

資料來源(只有兩個,沒有第三個):
  1. Shioaji 訂閱串流 —— 價、量、aflow、內外盤、當日漲跌幅
  2. DB 昨日死值 —— 昨高、月線、量能基準

盤中一次都不打 FinMind。所有法人/融資欄位都是昨日死值,早躺在 DB 裡。
把 FinMind key 清空,盤中燈號照樣出 —— 這是驗收條件。

系統只給燈號,永不下單。綠燈的意思是「到你預設條件了」,
實際進場仍由你按 ATR 規則決定。系統不替你扣扳機。
"""

from __future__ import annotations

import datetime as _dt
import json

import store
import screen_post
from envelope import run_all, persist_status, missing_labels
from phase import Phase, now_tw, prev_trading_day, today_tw

PLUGIN = "screen_intraday"
TABLE = "intraday_signal"

# ---------------------------------------------------------------- 綠燈四條件
GREEN_REQUIRED = 4          # 四條件全中才綠燈。少一個就是黃燈。
BID_ASK_MIN = 1.2           # 內外盤比門檻
VOLUME_PACE_MIN = 1.0       # 量能較昨日同時段的倍數

# ---------------------------------------------------------------- 三條鐵律
OPENING_BLIND_MIN = 15      # 鐵律1:開盤前 15 分鐘的 aflow 負值一律忽略
SURGE_PCT = 8.0             # 鐵律2:漲幅超過此值視為噴漲,aflow 負數不判紅燈


def _minutes_since_open(at: _dt.datetime | None = None) -> int:
    at = at or now_tw()
    return (at.hour - 9) * 60 + at.minute


def judge_one(code: str, pool_row: dict, quote: dict | None, aflow: dict | None,
              bar_y: dict | None, at: _dt.datetime | None = None) -> dict:
    """
    單檔嚴判。任何參數為 None 都不會爆,只是該條件不成立(不是判紅燈)。
    """
    mins = _minutes_since_open(at)
    price = (quote or {}).get("price")
    cr = (quote or {}).get("change_rate")
    vol = (quote or {}).get("volume")
    bid = (quote or {}).get("bid_vol")
    ask = (quote or {}).get("ask_vol")
    na = (aflow or {}).get("net_active")
    y_high = (bar_y or {}).get("high")
    ma20 = (bar_y or {}).get("ma20")
    y_vol = (bar_y or {}).get("volume")

    conds: dict[str, bool | None] = {}
    notes: list[str] = []
    missing: list[str] = []

    # 條件1:站上昨高
    if price is not None and y_high:
        conds["站上昨高"] = price >= y_high
    else:
        conds["站上昨高"] = None
        missing.append("昨高")

    # 條件2:aflow 轉正
    if na is not None:
        conds["主動買超"] = na > 0
    else:
        conds["主動買超"] = None
        missing.append("aflow")

    # 條件3:內外盤比 > 1.2
    if bid and ask:
        conds["內外盤比"] = (bid / ask) >= BID_ASK_MIN
    else:
        conds["內外盤比"] = None
        missing.append("內外盤")

    # 條件4:量能較昨日同時段放大
    if vol is not None and y_vol and mins > 0:
        session_frac = min(1.0, mins / 270)   # 09:00–13:30 共 270 分鐘
        pace = vol / max(1.0, y_vol * session_frac)
        conds["量能跟上"] = pace >= VOLUME_PACE_MIN
    else:
        conds["量能跟上"] = None
        missing.append("量能")

    hit = sum(1 for v in conds.values() if v is True)

    # ---------------------------------------------------------- 紅燈判定
    #
    # 鐵律1:開盤前 15 分鐘的 aflow 負值一律忽略。
    #        6/17 被動元件案例:9:05 顯示 −171.12 卻是主力開盤壓低吸貨,
    #        個股隨後 V 轉漲停。開盤負值是壓低吸籌,不是出貨。
    #
    # 鐵律2:噴漲檔的 aflow 負數不判紅燈。
    #        接近漲停時委賣掛單造成的 aflow 負數是排隊要買,不是出貨。
    #        這種標黃燈續盯。
    #
    # 鐵律3(盤中總則):intraday 的資金流向是主動買賣推估,不是法人買賣超。
    #        盤中 outflow ≠ 出貨。要等盤後法人蓋章才算數。

    red = False
    if price is not None and ma20 and price < ma20:
        red = True
        notes.append("跌破月線")

    if na is not None and na < 0:
        if mins < OPENING_BLIND_MIN:
            notes.append(f"開盤{mins}分內 aflow 負值,依鐵律忽略(可能是壓低吸籌)")
        elif cr is not None and cr >= SURGE_PCT:
            notes.append("噴漲中 aflow 負數 = 排隊掛買,不判死")
        else:
            # 持續且帶量的負值才算真的轉弱
            if vol is not None and y_vol and vol > y_vol * 0.8:
                red = True
                notes.append("aflow 持續轉負且帶量")
            else:
                notes.append("aflow 負但量未放大,續觀察")

    # ---------------------------------------------------------- 燈號
    if red:
        light = "🔴"
        # 紅燈不可只顯示結論，必須把觸發淘汰的實際原因帶到列表，
        # 避免看到「移出」卻不知道是跌破月線還是資金轉弱。
        reason = "、".join(notes) or "觸發風險條件"
        action = f"移出今日視線：{reason}"
    elif hit >= GREEN_REQUIRED:
        light = "🟢"
        action = f"到預設進場條件:{pool_row.get('entry_rule') or '—'}"
    else:
        light = "🟡"
        action = f"續盯,四條件中 {hit} 項"

    return {
        "code": code,
        "light": light,
        "track": pool_row.get("track"),
        "rank": pool_row.get("rank"),
        "post_score": pool_row.get("score"),
        "trigger_price": pool_row.get("trigger_price"),
        "entry_rule": pool_row.get("entry_rule"),
        "price": price, "change_rate": cr, "net_active": na,
        "conditions": conds, "hit": hit,
        "action": action, "notes": notes, "missing": missing,
        "has_data": quote is not None,
    }


def build(db_path: str = "mls.db", at: _dt.datetime | None = None) -> dict:
    """
    盤中只盯昨日盤後產出的候選池,不掃全市場。
    任何一格資料缺失都不會導致燈號出不來。
    """
    # 這支從頭到尾只讀 SQLite,不呼叫任何取數函式 —— 所以不需要守門。
    # 守門(assert_can_fetch_finmind)裝在 FinMind 取數函式那一端。
    today = today_tw()
    yday = prev_trading_day()

    envs = run_all({
        "pool": lambda: screen_post.load_pool(yday, db_path),
        "quote": lambda: store.read_date("quote_snap", today, db_path),
        "aflow": lambda: store.read_date("aflow", today, db_path),
        "bar_y": lambda: store.read_date("daily_bar", yday, db_path),
    }, phase=Phase.INTRADAY)
    persist_status(envs, db_path)

    pool = envs["pool"].get({}) or {}
    if not pool:
        return {
            "phase": "INTRADAY", "data_date": today.isoformat(),
            "purpose": "盤中嚴判 — 昨日候選池尚未產生,請先跑盤後篩選",
            "actionable": False, "degraded": ["候選池"], "items": [],
        }

    q = envs["quote"].get({}) or {}
    a = envs["aflow"].get({}) or {}
    b = envs["bar_y"].get({}) or {}

    items = []
    for code, row in pool.items():
        pr = dict(row)
        if pr.get("payload"):
            try:
                pr.update(json.loads(pr["payload"]))
            except Exception:
                pass
        items.append(judge_one(code, pr, q.get(code), a.get(code), b.get(code), at))

    order = {"🟢": 0, "🟡": 1, "🔴": 2}
    items.sort(key=lambda x: (order[x["light"]], -x["hit"], x.get("rank") or 999))

    gen = _dt.datetime.now().isoformat(timespec="seconds")
    store.upsert_intraday(TABLE, PLUGIN, [{
        "data_date": today.isoformat(), "code": it["code"],
        "light": it["light"],
        "conditions": json.dumps(it["conditions"], ensure_ascii=False),
        "note": json.dumps(it["notes"], ensure_ascii=False),
        "updated_at": gen,
    } for it in items], db_path)

    counts = {k: sum(1 for i in items if i["light"] == k) for k in ("🟢", "🟡", "🔴")}

    return {
        "phase": "INTRADAY", "data_date": today.isoformat(),
        "pool_date": yday.isoformat(),
        "purpose": (f"盤中嚴判(候選池 {yday},{len(items)} 檔)— "
                    f"綠燈 = 到你預設進場條件,系統不代為下單"),
        "actionable": False,
        "generated_at": gen,
        "degraded": missing_labels(envs),
        "light_counts": counts,
        "items": items,
    }
