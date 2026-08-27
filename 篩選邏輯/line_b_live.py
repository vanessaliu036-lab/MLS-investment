"""line_b_live.py — Line B 盤中即時合成層(唯讀,完全不寫 DB)。

盤中(phase.get_phase()==INTRADAY)專用:今晚的 EOD ledger(run_line_b_ledger.py)
還沒跑,但使用者現在就要看「進行式」的狀態。這支只是把已經存在、已經測過的邏輯
兜起來看「現在」,不重寫第二套判斷:

  · T 日候選名單(C1/C2/t1_prior_high/t1_ma20)= 直接呼叫
    run_line_b_ledger._c1_c2(),跟收盤後 EOD 用的是同一支函式,只是 T1 動態抓
    「今天以前最新的一個交易日收盤」(今天自己的收盤本來就還沒有,本來就不需要)。
  · T 日盤中狀態(flow_class/confirm_magnitude/activation)= 直接呼叫
    run_line_b_ledger._flow_and_activation(),吃「今天已經記錄進 b_snapshot 的
    歷史格(5分鐘一格)+ 當下 quote_snap/aflow 最新一筆」兜成的序列。這支函式本來
    就是「吃到哪算到哪」——只要餵給它的序列本身不包含未來,它天生就是
    point-in-time 正確,不需要另外重寫一份「即時版」的確認邏輯。
  · 當下最新一筆 = snapshot_producer.build_buffer(),跟盤中每 5 分鐘落地
    b_snapshot 用的是同一支函式、同一份 production feed(quote_snap+aflow),
    不另造第二套 feed。

A-flow freshness(2026-08-26/27 新增,第一個真的「用」freshness 欄位做判斷的地方):
  若當下這筆 aflow 讀數 stale(見 _is_aflow_stale),就把這格的 net_active 當
  None 餵進 _flow_and_activation——效果等同「這一格還沒有新資訊」,不會把一個
  過期/凍結的數字誤判成新的 OPEN_POSITIVE/FLOW_FLIP 或觸發活化。已經在更早、
  資料還新鮮時就成立的 flow_class/activation 不會因為現在斷流而消失(那是已經
  發生的事實,不是「拿舊數字生新判斷」)。UI 端另外用 flow_stale 旗標決定要不要
  把资金那一行改顯示「資金資料待更新」(見 line_b_explain.explain 的 flow_stale
  參數)。

⚠ 兩支時鐘不能混用(見 memory clock-is-not-calendar):
  · 台灣時段/交易日判斷 → phase.py(now_tw/get_phase/today_tw),唯一權威。
  · updated_at 新鮮度比較 → 跟 feed_bridge.py/snapshot_producer.py 寫入時同一種
    時鐘(未加 tz 的 datetime.now(),VPS 系統時區固定,只用來跟同一批 updated_at
    互相比較「差幾秒」,不能拿來判斷現在台灣幾點)。
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Optional

import phase
import run_line_b_ledger as _runner
import snapshot_producer as _producer
import line_b_explain as _explain

DB = "mls.db"

# A-flow 新鮮度門檻(2026-08-27 工程判斷,可逆,非研究門檻):feed_bridge.py 盤中
# LIVE_INTERVAL=30 秒輪詢一次;超過 6 輪(180 秒)沒收到新的 aflow 落地,或
# quote/aflow 兩者 updated_at 互相差超過 180 秒,判定 A-flow 這一格 stale——
# 不讓它更新 confirmed_so_far/啟動機率,只降級顯示,不假裝正常。
STALE_AFLOW_AGE_SEC = 180
STALE_GAP_SEC = 180


def _parse_ts(s: Optional[str]):
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def _naive_now() -> _dt.datetime:
    return _dt.datetime.now()


def is_aflow_stale(quote_updated_at: Optional[str], aflow_updated_at: Optional[str],
                   now: Optional[_dt.datetime] = None) -> bool:
    now = now or _naive_now()
    a = _parse_ts(aflow_updated_at)
    if a is None:
        return True
    if (now - a).total_seconds() > STALE_AFLOW_AGE_SEC:
        return True
    q = _parse_ts(quote_updated_at)
    if q is not None and abs((q - a).total_seconds()) > STALE_GAP_SEC:
        return True
    return False


def _latest_t1(db_path: str, T: str) -> Optional[str]:
    """T 之前最新一個有 daily_bar 收盤的日期。今天(T)自己的收盤本來就還沒有,
    這是預期行為,不是缺資料。"""
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT MAX(data_date) FROM daily_bar WHERE data_date<?", (T,)
        ).fetchone()
    return row[0] if row else None


def build_live_rows(db_path: str = DB, T: Optional[str] = None) -> dict:
    """回傳 { 'T':.., 'T1':.., 'rows': [ {code, source, flow_confirm_magnitude,
    watch_mode_activated, explain} ... ] }。完全不寫 DB。

    只在 phase.get_phase()==INTRADAY 才有意義被呼叫(呼叫端自己判斷 phase;這支
    本身不擋,方便測試用固定 T/db 重播)。
    """
    T = T or phase.today_tw().isoformat()
    T1 = _latest_t1(db_path, T)
    if T1 is None:
        return {"T": T, "T1": None, "rows": [], "skipped": "no prior daily_bar close"}

    codes = _runner._universe()
    live_buf = _producer.build_buffer(db_path)  # 同一份 production feed,不另造第二套
    now = _naive_now()

    rows_out = []
    for code in codes:
        inst_rows = _runner._history(db_path, "inst_flow", code, T1)
        bar_rows = _runner._history(db_path, "daily_bar", code, T1)
        if len(inst_rows) < 6 or len(bar_rows) < 6:
            continue
        aflow_rows = _runner._history(db_path, "aflow", code, T1)
        c1, c2, t1_fields = _runner._c1_c2(inst_rows, bar_rows, aflow_rows)
        t1_ma20, t1_prior_high = t1_fields["t1_ma20"], t1_fields["t1_prior_high"]

        snap_rows = list(_runner._snapshot_rows(db_path, code, T))
        tick = live_buf.get(code) or {}
        flow_stale = is_aflow_stale(tick.get("quote_updated_at"), tick.get("aflow_updated_at"), now)
        if tick.get("price") is not None:
            snap_rows.append({
                "slot": phase.now_tw().strftime("%H%M"),
                "price": tick.get("price"),
                "net_active": None if flow_stale else tick.get("net_active"),
            })

        if not snap_rows or t1_ma20 is None or t1_prior_high is None:
            flow_class, confirm_mag, activated, act_slot = None, None, False, None
            hi = lo = close_t = None
        else:
            flow_class, confirm_mag, activated, act_slot, hi, lo, close_t = \
                _runner._flow_and_activation(snap_rows, t1_ma20, t1_prior_high)

        if not (c1 and c2) and not activated:
            continue

        source = "C1C2_PASS" if (c1 and c2) else "INTRADAY_DISCOVERY"
        current_price = close_t if close_t is not None else tick.get("price")
        row = dict(
            code=code, source=source,
            t1_close=t1_fields.get("t1_close"),
            t1_ma20=t1_ma20, t1_prior_high=t1_prior_high,
            t1_inst_5d=t1_fields.get("t1_inst_5d"), t1_price_5d=t1_fields.get("t1_price_5d"),
            t1_close_position=t1_fields.get("t1_close_position"),
            flow_class=flow_class, flow_confirm_magnitude=confirm_mag,
            watch_mode_activated=int(bool(activated)), activation_slot=act_slot,
            current_price=current_price,
        )
        row["explain"] = _explain.explain(row, is_eod=False, flow_stale=flow_stale)
        row["flow_confirm_magnitude"] = confirm_mag
        rows_out.append(row)

    return {"T": T, "T1": T1, "rows": rows_out}
