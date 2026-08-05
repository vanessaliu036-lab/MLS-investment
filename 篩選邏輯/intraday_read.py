"""
intraday_read.py — 盤中白話解析(程式模板,不呼叫 AI)

你要看到的不是一堆數字讓你自己猜,是一句話說清楚現在的狀況。
最重要的那一句:「股價漲、資金流出、大戶邊買邊拉」。

===== 為什麼盤中不能廢掉 =====

盤後籌碼面有結構性盲點:它是淨額。
法人買超 2000 張,可能是買 5000 賣 3000,也可能是買 2100 賣 100。
同一個數字,兩種完全不同的意義 —— 淨額把「誰在賣」這個資訊消掉了。

盤中資金流補的正是這個。
漲停但資金流出,意思是價格被少數買盤推上去,整體有人趁高出貨。
這個矛盾在盤後數字上完全看不出來。

===== 誠實聲明:抓不到「大戶」 =====

Shioaji 給的是價、量、內外盤、逐筆成交,裡面沒有身分。
所以「大戶」只能從行為推:大單佔比、拉抬時的單筆規模。

這是推估,不是事實 —— 就像 aflow 不是法人買賣超一樣。
本模組所有涉及「大戶」的敘述一律加「推估」二字,不寫確定句。

===== 四種狀態,只有兩種需要跳出來 =====

    價漲 + 資金流出  →  邊拉邊出 ⚠  (籌碼再漂亮也不追)
    價跌 + 資金流入  →  下殺有人接 ⚠ (6/17 形狀)
    價漲 + 資金流入  →  正常,不特別提示
    價跌 + 資金流出  →  正常走弱,不特別提示
"""

from __future__ import annotations

import datetime as _dt

BLIND_MIN = 15              # 鐵律1:開盤 15 分鐘的資料不納入判讀
UP_PCT = 1.0                # 視為「漲」的門檻
DOWN_PCT = -1.0             # 視為「跌」的門檻
BIG_ORDER_MULT = 3.0        # 單一區間量能超過均值此倍數 → 推估為大單
SURGE_PCT = 8.0             # 噴漲門檻(鐵律2:此時 aflow 負值不判死)


def _usable(series: list[dict]) -> list[dict]:
    return [s for s in series
            if int(s["slot"][:2]) * 60 + int(s["slot"][2:]) >= 9 * 60 + BLIND_MIN]


def _wan(x: float | None) -> str:
    """金額轉萬元,帶正負號。"""
    if x is None:
        return "—"
    return f"{x/1e4:+,.0f} 萬"


def _big_order_slots(series: list[dict]) -> list[dict]:
    """
    推估大單出現的時段:某區間的增量遠高於當日均值。
    這是行為推估,不是身分辨識 —— 資料裡沒有「大戶」這個欄位。
    """
    s = _usable(series)
    vols = [(x, x.get("volume")) for x in s if x.get("volume") is not None]
    if len(vols) < 4:
        return []
    deltas = []
    for i in range(1, len(vols)):
        deltas.append((vols[i][0], max(0, vols[i][1] - vols[i - 1][1])))
    avg = sum(d for _, d in deltas) / len(deltas)
    if avg <= 0:
        return []
    return [x for x, d in deltas if d >= avg * BIG_ORDER_MULT]


def read_one(code: str, name: str | None, series: list[dict],
             bar_y: dict | None = None) -> dict:
    """產出單檔的白話判讀。"""
    s = _usable(series)
    if not s:
        return {"code": code, "state": "NO_DATA", "flag": False,
                "headline": "盤中資料不足,無法判讀", "detail": "", "missing": ["時序"]}

    last = s[-1]
    cr = last.get("change_rate")
    na = last.get("net_active")
    price = last.get("price")
    mins = int(last["slot"][:2]) * 60 + int(last["slot"][2:]) - 9 * 60

    missing = []
    if cr is None:
        missing.append("漲跌幅")
    if na is None:
        missing.append("資金流")

    if cr is None or na is None:
        return {"code": code, "name": name, "state": "NO_DATA", "flag": False,
                "headline": "缺少" + "、".join(missing) + ",不判讀",
                "detail": "", "price": price, "change_rate": cr,
                "net_active": na, "missing": missing}

    # ---- 資金流轉折時點
    turn = None
    for x in s:
        if x.get("net_active") is not None and x["net_active"] < 0:
            turn = x["slot"]
            break

    big = _big_order_slots(series)
    big_on_up = 0
    for x in big:
        c = x.get("change_rate")
        if c is not None and c > 0:
            big_on_up += 1

    # ---------------------------------------------------------- 四種狀態
    if cr >= UP_PCT and na < 0:
        state, flag = "邊拉邊出", True
        head = f"股價漲 {cr:+.1f}%,但資金淨流出 {_wan(na)}"
        parts = []
        if big_on_up >= 2:
            parts.append(f"漲幅推估來自 {big_on_up} 個大單時段,"
                         f"而整體主動賣壓大於買盤")
        else:
            parts.append("價格上行但主動賣壓大於買盤")
        if turn:
            parts.append(f"{turn[:2]}:{turn[2:]} 起資金轉為流出")
        if cr >= SURGE_PCT:
            parts.append("注意:接近漲停時的委賣掛單也會造成流出讀數,"
                         "此處為推估,需盤後法人蓋章確認")
        else:
            parts.append("籌碼面再漂亮也不宜追價,待盤後確認誰在賣")
        detail = "。".join(parts) + "。"

    elif cr <= DOWN_PCT and na > 0:
        state, flag = "下殺有人接", True
        head = f"股價跌 {cr:+.1f}%,但資金淨流入 {_wan(na)}"
        parts = ["下殺過程有人主動承接"]
        if mins < 30:
            parts.append("開盤初期壓低,可能是吸籌,但時間尚短")
        parts.append("這是推估值,非法人買賣超,須待盤後蓋章")
        detail = "。".join(parts) + "。"

    elif cr >= UP_PCT and na >= 0:
        state, flag = "量價一致偏多", False
        head = f"股價漲 {cr:+.1f}%,資金淨流入 {_wan(na)}"
        detail = "價格與資金方向一致,漲勢有主動買盤支撐。"

    elif cr <= DOWN_PCT and na <= 0:
        state, flag = "量價一致偏空", False
        head = f"股價跌 {cr:+.1f}%,資金淨流出 {_wan(na)}"
        detail = "價格與資金方向一致,單純走弱,無人承接。"

    else:
        state, flag = "盤整", False
        head = f"股價 {cr:+.1f}%,資金 {_wan(na)}"
        detail = "價格與資金皆無明顯方向。"

    return {
        "code": code, "name": name, "state": state, "flag": flag,
        "headline": head, "detail": detail,
        "price": price, "change_rate": cr, "net_active": na,
        "big_order_slots": [x["slot"] for x in big],
        "missing": missing,
    }


def read_all(snaps: dict[str, list[dict]], names: dict[str, str] | None = None,
             only_flagged: bool = True) -> dict:
    """
    全體判讀。

    only_flagged=True → 只回傳需要注意的兩種(邊拉邊出、下殺有人接)。
                        量價一致的不佔版面。
    """
    names = names or {}
    out = [read_one(c, names.get(c), ser) for c, ser in snaps.items()]

    counts: dict[str, int] = {}
    for o in out:
        counts[o["state"]] = counts.get(o["state"], 0) + 1

    items = [o for o in out if o["flag"]] if only_flagged else out
    order = {"邊拉邊出": 0, "下殺有人接": 1}
    items.sort(key=lambda x: (order.get(x["state"], 9),
                              -abs(x.get("change_rate") or 0)))

    return {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "state_counts": counts,
        "flagged": len(items),
        "note": ("「大戶」為行為推估(大單時段佔比),資料中無身分欄位;"
                 "資金流為主動買賣推估,非法人買賣超。"),
        "items": items,
    }
