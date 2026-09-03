"""
intraday_read.py — 盤中白話解析(程式模板,不呼叫 AI)

你要看到的不是一堆數字讓你自己猜,是一句話說清楚現在的狀況。
最重要的那一句:「股價漲、主動買賣差流出、推估有大單拉抬」。

===== 為什麼盤中不能廢掉 =====

盤後籌碼面有結構性盲點:它是淨額。
法人買超 2000 張,可能是買 5000 賣 3000,也可能是買 2100 賣 100。
同一個數字,兩種完全不同的意義 —— 淨額把「誰在賣」這個資訊消掉了。

盤中 A-flow 補的是成交積極度方向,不是法人身分。
漲停但 A-flow 流出,代表可觀察到的主動賣成交量大於主動買成交量；
不能直接翻譯成「法人出貨」,仍需等盤後官方籌碼蓋章。

===== 誠實聲明:抓不到「大戶」 =====

Shioaji 給的是價、量、內外盤、逐筆成交,裡面沒有身分。
所以「大戶」只能從行為推:大單佔比、拉抬時的單筆規模。

這是推估,不是事實 —— 就像 aflow 不是法人買賣超一樣。
本模組所有涉及「大戶」的敘述一律加「推估」二字,不寫確定句。

===== 四種狀態,只有兩種需要跳出來 =====

    價漲 + A-flow流出  →  邊拉邊出 ⚠  (籌碼再漂亮也不追)
    價跌 + A-flow流入  →  下殺有人接 ⚠ (6/17 形狀)
    價漲 + A-flow流入  →  價流一致偏多,不特別提示
    價跌 + A-flow流出  →  價流一致偏空,不特別提示
"""

from __future__ import annotations

import datetime as _dt

import intraday_metric_contract

BLIND_MIN = 15              # 鐵律1:開盤 15 分鐘的資料不納入判讀
UP_PCT = 1.0                # 視為「漲」的門檻
DOWN_PCT = -1.0             # 視為「跌」的門檻
BIG_ORDER_MULT = 3.0        # 單一區間量能超過均值此倍數 → 推估為大單
SURGE_PCT = 8.0             # 噴漲門檻(鐵律2:此時 aflow 負值不判死)


def _usable(series: list[dict]) -> list[dict]:
    return [s for s in series
            if int(s["slot"][:2]) * 60 + int(s["slot"][2:]) >= 9 * 60 + BLIND_MIN]


def _lots(x: float | None) -> str:
    """A-flow 為主動買量−主動賣量,單位固定用張,不得格式化成金額。"""
    if x is None:
        return "—"
    return f"{x:+,.0f} 張"


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
    metrics = intraday_metric_contract.normalize({
        "volume": last.get("volume"),
        "aflow": na,
        "aflow_status": last.get("aflow_status") or "LIVE",
    })

    missing = []
    if cr is None:
        missing.append("漲跌幅")
    if na is None:
        missing.append("主動買賣差(A-flow)")

    if cr is None or na is None:
        return {"code": code, "name": name, "state": "NO_DATA", "flag": False,
                "headline": "缺少" + "、".join(missing) + ",不判讀",
                "detail": "", "price": price, "change_rate": cr,
                "net_active": na, "metric_contract": metrics, "missing": missing}

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
        head = f"股價漲 {cr:+.1f}%,但 A-flow {_lots(na)}"
        parts = []
        if big_on_up >= 2:
            parts.append(f"漲幅推估來自 {big_on_up} 個大單時段,"
                         f"而整體主動賣成交量大於主動買成交量")
        else:
            parts.append("價格上行但主動賣成交量大於主動買成交量")
        if turn:
            parts.append(f"{turn[:2]}:{turn[2:]} 起 A-flow 轉負")
        if cr >= SURGE_PCT:
            parts.append("注意:接近漲停時委賣結構也可能造成負讀數,"
                         "此處為盤中推估,需盤後法人蓋章確認")
        else:
            parts.append("A-flow 只代表成交積極度,非法人出貨證據;待盤後確認")
        detail = "。".join(parts) + "。"

    elif cr <= DOWN_PCT and na > 0:
        state, flag = "下殺有人接", True
        head = f"股價跌 {cr:+.1f}%,但 A-flow {_lots(na)}"
        parts = ["下殺過程主動買成交量大於主動賣成交量"]
        if mins < 30:
            parts.append("開盤初期壓低,可能是吸籌,但時間尚短")
        parts.append("這是盤中主動買賣差,非法人買賣超,須待盤後蓋章")
        detail = "。".join(parts) + "。"

    elif cr >= UP_PCT and na >= 0:
        state, flag = "價流一致偏多", False
        head = f"股價漲 {cr:+.1f}%,A-flow {_lots(na)}"
        detail = "價格與主動買賣差方向一致,漲勢有主動買成交支撐。"

    elif cr <= DOWN_PCT and na <= 0:
        state, flag = "價流一致偏空", False
        head = f"股價跌 {cr:+.1f}%,A-flow {_lots(na)}"
        detail = "價格與主動買賣差方向一致,短線成交積極度偏弱。"

    else:
        state, flag = "盤整", False
        head = f"股價 {cr:+.1f}%,A-flow {_lots(na)}"
        detail = "價格與主動買賣差皆無明顯方向。"

    return {
        "code": code, "name": name, "state": state, "flag": flag,
        "headline": head, "detail": detail,
        "price": price, "change_rate": cr, "net_active": na,
        "metric_contract": metrics,
        "big_order_slots": [x["slot"] for x in big],
        "missing": missing,
    }


def read_all(snaps: dict[str, list[dict]], names: dict[str, str] | None = None,
             only_flagged: bool = True) -> dict:
    """
    全體判讀。

    only_flagged=True → 只回傳需要注意的兩種(邊拉邊出、下殺有人接)。
                        其餘狀態不佔版面。
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
        "note": ("A-flow 為主動買量−主動賣量,單位張,非法人買賣超;"
                 "「大戶」為行為推估,資料中無身分欄位。"),
        "field_contract_version": "intraday-metrics-v1",
        "items": items,
    }
