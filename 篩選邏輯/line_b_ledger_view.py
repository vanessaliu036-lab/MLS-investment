"""Line B Watch Ledger — 呈現層,唯讀。

⚠ 這支檔案不做任何判定/計分。C1/C2/flow_class/watch_mode_activated 全部
直接讀 `line_b_watch_ledger`(由 run_line_b_ledger.py 每日收盤後寫入)的
既有欄位,不重算、不重新排序證據強度、不套新門檻。

三塊語意(2026-08-26 Vanessa 定案,不得混算):
  1. C1+C2 盤後通過名單 — 標「Historical 64.1%」,樣本 = 2026-08-26
     One-Shot Acceptance 封板時的乾淨歷史窗(11天/n=561/day-equal)。
  2. A-flow CONFIRMED — 從 (1) 名單內再篩 flow_class != NO_FLIP,依
     flow_confirm_magnitude(A-flow 幅度,不是時間點)排序取前3,
     標「Historical 89.9%」,同一個歷史樣本。
  3. Intraday Discovery — source=INTRADAY_DISCOVERY,即使昨晚未過 C1+C2
     也因為今天盤中觸發 WATCH MODE 而出現。**必須**標
     "INTRADAY DISCOVERY — not part of the 64.1%/89.9% validated sample"，
     不得跟前兩塊的既有驗證數字混算或暗示同一個 base rate。

時序鐵律:本檔只 SELECT 當天(或指定日)的列,不回填、不跨日重算 T-1 欄位
——那些欄位本來就是 run_line_b_ledger.py 寫入當下就凍結的。
"""
from __future__ import annotations
import sqlite3
from typing import Optional

import line_b_explain as _explain

HISTORICAL_LABELS = {
    "c1_c2_rate": "64.1%",
    "flow_confirmed_rate": "89.9%",
    "flow_no_flip_rate": "2.8%",
    "sample_note": "11 clean days · n=561 · day-equal · 2026-08-26 One-Shot Acceptance",
    "caveat": ("Retrospective result on the available clean-day sample at freeze time. "
              "Not a forward guarantee — this ledger exists to track whether it holds up."),
}


def _row_dict(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# 排序(2026-08-26 Vanessa 更正,2026-08-27 抽成共用函式給 EOD/live 兩條路徑共用):
# 保留狀態分區(CONFIRMED > WATCH_CLOSELY > WAIT > GIVE_UP 仍分區塊呈現),但每個
# 分區內的主排序是即時 net_active 幅度(flow_confirm_magnitude),距離關鍵價只做
# 次排序 / 卡片內容,不當主排序依據。⚠ 之前誤把「距離」當主排序鍵,已更正——
# 不要再改回去。flow_class 是否已確認也不影響排序(那是 status 分區的事)。
_STATUS_ORDER = {"CONFIRMED": 0, "WATCH_CLOSELY": 1, "WAIT": 2, "GIVE_UP": 3}


def _sort_key(r: dict):
    e = r["explain"]
    dist = e.get("distance_pct")
    return (_STATUS_ORDER.get(e["status"], 9), -(r.get("flow_confirm_magnitude") or 0),
            -(dist if dist is not None else -999))


def _finalize(rows: list[dict], data_date: Optional[str]) -> dict:
    """rows 每個元素需含 'source'/'explain'/'flow_confirm_magnitude'/'flow_class'。
    EOD 與 live 兩條路徑最後都走這裡,排序/分桶邏輯只有一份。"""
    c1_c2_list = [r for r in rows if r["source"] == "C1C2_PASS"]
    intraday_discovery = [r for r in rows if r["source"] == "INTRADAY_DISCOVERY"]

    c1_c2_list.sort(key=_sort_key)
    intraday_discovery.sort(key=lambda r: -(r.get("flow_confirm_magnitude") or 0))

    confirmed = [r for r in c1_c2_list if r.get("flow_class") in ("OPEN_POSITIVE", "FLOW_FLIP")]
    confirmed_ranked = sorted(
        confirmed, key=lambda r: (r.get("flow_confirm_magnitude") or 0), reverse=True,
    )
    flow_confirmed_top3 = confirmed_ranked[:3]

    return dict(
        data_date=data_date, has_data=len(rows) > 0,
        c1_c2_list=c1_c2_list,
        flow_confirmed_top3=flow_confirmed_top3,
        intraday_discovery=intraday_discovery,
        counts=dict(c1_c2=len(c1_c2_list), confirmed=len(confirmed),
                   discovery=len(intraday_discovery)),
        labels=HISTORICAL_LABELS,
    )


def build_ledger_context(data_date: Optional[str] = None, db_path: str = "mls.db") -> dict:
    conn = sqlite3.connect(db_path)
    try:
        if data_date is None:
            row = conn.execute("SELECT MAX(data_date) FROM line_b_watch_ledger").fetchone()
            data_date = row[0] if row else None
        if data_date is None:
            return dict(data_date=None, has_data=False, c1_c2_list=[], flow_confirmed_top3=[],
                       intraday_discovery=[], labels=HISTORICAL_LABELS)

        rows = _row_dict(
            conn,
            "SELECT * FROM line_b_watch_ledger WHERE data_date=? ORDER BY code",
            (data_date,),
        )
    finally:
        conn.close()

    # is_eod=True: ledger 只在收盤後寫入,這裡讀到的一律是完整一天,GIVE_UP 判定
    # 才有意義(盤中即時版走 build_live_context,is_eod=False,不在這支範圍)。
    for r in rows:
        r["explain"] = _explain.explain(r, is_eod=True)

    ctx = _finalize(rows, data_date)
    ctx["is_live"] = False
    return ctx


def build_live_context(db_path: str = "mls.db", T: Optional[str] = None) -> dict:
    """盤中即時版——只在 phase.get_phase()==INTRADAY 有意義(呼叫端
    line_b_ledger_api.py 自己判斷 phase 決定要不要走這條路徑)。完全不寫 DB,
    每次呼叫即時組裝(見 line_b_live.build_live_rows)。"""
    import line_b_live as _live

    result = _live.build_live_rows(db_path, T)
    rows = result["rows"]
    ctx = _finalize(rows, result["T"])
    ctx["is_live"] = True
    ctx["t1_used"] = result.get("T1")
    return ctx
