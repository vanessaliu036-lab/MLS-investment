"""run_line_b_ledger.py — 每交易日收盤後跑一次,把 C1+C2 名單 + 盤中確認狀態
+ 盤中額外冒出的股票,寫進 line_b_watch_ledger(append-only)。

⚠ 研究產物,不是 production gate:
   - 不讀 A 鏈任何表,不寫 candidate_pool/watchlist_post
   - 完全不影響現有 tier/track/score/UI
   - 只在收盤後跑一次(讀 daily_bar/inst_flow 的 T-1 收盤 + b_snapshot 的 T 日盤中)

執行順序,對應 Vanessa 的循環設計:
  1. 用 T-1(上一交易日)收盤資料算 C1(結構未破)+ C2(賣壓減弱+價格反應)
     → 通過的 code,source=C1C2_PASS
  2. 用 T 日 b_snapshot 算 flow_class(OPEN_POSITIVE/FLOW_FLIP/NO_FLIP,事件制非時間點)
     + WATCH MODE activation(confirmed_reversal 逐格判定)
  3. T 日「有 activation 但昨晚不在 C1+C2 名單」的 code → 額外開一列,
     source=INTRADAY_DISCOVERY,T-1 欄位全部留 NULL(時序鎖:不能假裝昨晚就選中)
  4. 用 T 日收盤資料重算 EOD C1/C2 → enters_next_day_watchlist,供明天用

只呼叫一次 line_b_watch_ledger.write_rows(),語意不變則 no-op、語意變了拒絕覆寫
(見該模組 docstring)。
"""
from __future__ import annotations

import sys
import datetime as _dt

sys.path.insert(0, "/Users/vanessaliu/Desktop/mls-intraday/篩選邏輯")

import store
import chip_price_divergence as CPD
import line_b_watch_ledger as ledger
from phase import today_tw

DB = "mls.db"
SIG_SELL_LOTS = 3000


def _c1_c2(inst_rows, bar_rows, aflow_rows):
    """回傳 (C1, C2, 供 ledger 存的原始欄位 dict)。inst_rows/bar_rows 需 newest-first。"""
    res = CPD.scan(inst_rows, bar_rows, aflow_rows)
    m = res["divergence_metrics"]
    today_bar = bar_rows[0] if bar_rows else {}
    close, ma20 = CPD._num(today_bar.get("close")), CPD._num(today_bar.get("ma20"))
    # bar_rows[0] IS T-1 itself (newest-first history "as of T-1") -> T-1's own high
    # is "昨高" from T's perspective. bar_rows[1] would be T-2, which is wrong.
    prior_high = CPD._num(today_bar.get("high"))
    c1 = close is not None and ma20 is not None and close >= ma20
    sig_sell = m["institution_5d"] is not None and m["institution_5d"] <= -SIG_SELL_LOTS
    c2 = (m["price_return_5d"] is not None and m["price_return_5d"] > 0 and
          m["close_position"] is not None and m["close_position"] >= 0.7 and not sig_sell)
    return c1, c2, dict(
        t1_close=close, t1_ma20=ma20, t1_prior_high=prior_high,
        t1_inst_5d=m["institution_5d"], t1_price_5d=m["price_return_5d"],
        t1_close_position=m["close_position"],
    )


BLIND_MIN_SLOT = "0915"  # b_discover.py 的 BLIND_MIN=15 鐵律:開盤 09:00/09:05/09:10
                        # 資料品質不可信(見 memory b-snapshot-2026-08-05-incident),
                        # 一律不納入判斷,不是可調參數。


def _flow_and_activation(snap_rows, t1_ma20, t1_high):
    """snap_rows: 當天 b_snapshot 按 slot 遞增排序的 list[dict]。
    回傳 (flow_class, flow_confirm_magnitude, activated, activation_slot, high, low, close)。
    """
    rows = [r for r in snap_rows if r.get("price") is not None and r.get("net_active") is not None
            and r.get("slot", "0000") >= BLIND_MIN_SLOT]
    if not rows:
        return None, None, False, None, None, None, None

    na = [r["net_active"] for r in rows]
    flow_class, confirm_mag = "NO_FLIP", None
    if na[0] > 0:
        flow_class, confirm_mag = "OPEN_POSITIVE", na[0]
    else:
        for i in range(len(na) - 1):
            if na[i] > 0 and na[i + 1] > 0:
                flow_class, confirm_mag = "FLOW_FLIP", na[i + 1]
                break

    activated, act_slot = False, None
    if t1_ma20 is not None and t1_high is not None:
        for r in rows:
            if r["price"] > t1_high and r["price"] >= t1_ma20 and r["net_active"] > 0:
                activated, act_slot = True, r["slot"]
                break

    prices = [r["price"] for r in rows]
    return flow_class, confirm_mag, activated, act_slot, max(prices), min(prices), prices[-1]


def run(data_date: _dt.date | None = None, db_path: str = DB) -> dict:
    d = data_date or today_tw()
    T = d.isoformat()

    with store.conn(db_path) as c:
        c.row_factory = None
        dates = [r[0] for r in c.execute(
            "SELECT DISTINCT data_date FROM daily_bar WHERE data_date<=? ORDER BY data_date DESC LIMIT 2", (T,)
        ).fetchall()]
    if len(dates) < 2 or dates[0] != T:
        return {"written": 0, "noop": 0, "skipped": f"no daily_bar close yet for {T}"}
    T1 = dates[1]

    codes = _universe()
    rows_out = []

    for code in codes:
        inst_rows = _history(db_path, "inst_flow", code, T1)
        bar_rows = _history(db_path, "daily_bar", code, T1)
        aflow_rows = _history(db_path, "aflow", code, T1)
        if len(inst_rows) < 6 or len(bar_rows) < 6:
            continue
        c1, c2, t1_fields = _c1_c2(inst_rows, bar_rows, aflow_rows)

        snap_rows = _snapshot_rows(db_path, code, T)
        flow_class, confirm_mag, activated, act_slot, hi, lo, close_t = _flow_and_activation(
            snap_rows, t1_fields["t1_ma20"], t1_fields["t1_prior_high"])

        if not (c1 and c2) and not activated:
            continue  # nothing to log: not a candidate, didn't activate either

        source = "C1C2_PASS" if (c1 and c2) else "INTRADAY_DISCOVERY"
        row = dict(code=code, source=source,
                  c1_structure_intact=int(c1) if source == "C1C2_PASS" else None,
                  c2_selling_weak_price_resp=int(c2) if source == "C1C2_PASS" else None,
                  **({k: v for k, v in t1_fields.items()} if source == "C1C2_PASS" else
                     {k: None for k in t1_fields}),
                  flow_class=flow_class, flow_confirm_magnitude=confirm_mag,
                  watch_mode_activated=int(activated), activation_slot=act_slot,
                  t_high=hi, t_low=lo, t_close=close_t)

        # EOD C1/C2 using TODAY's own close, for tomorrow's watchlist
        # "low" 不能漏——chip_price_divergence.scan() 的 close_position 要用
        # (close-low)/(high-low),漏了 low 會讓 close_position 恆為 None,
        # 導致 C2(close_position>=0.7)恆為 False,enters_next_day_watchlist
        # 永遠算不出 1(2026-08-27 發現:當天 32 檔全數 eod_c2=0)。
        bar_rows_incl_today = [{"close": close_t, "ma20": t1_fields["t1_ma20"],
                                "high": hi, "low": lo}] + bar_rows \
            if close_t is not None else bar_rows
        if close_t is not None and len(bar_rows_incl_today) >= 6:
            eod_c1, eod_c2, _ = _c1_c2(inst_rows, bar_rows_incl_today, aflow_rows)
        else:
            eod_c1, eod_c2 = None, None
        row["eod_c1"] = int(eod_c1) if eod_c1 is not None else None
        row["eod_c2"] = int(eod_c2) if eod_c2 is not None else None
        row["enters_next_day_watchlist"] = (int(bool(eod_c1) and bool(eod_c2))
                                            if eod_c1 is not None else None)
        rows_out.append(row)

    result = ledger.write_rows(d, rows_out, db_path)
    return {**result, "T": T, "T1": T1, "candidates": len(rows_out)}


def _universe() -> list[str]:
    from pathlib import Path
    ns: dict = {}
    exec((Path(__file__).parent / "config.py").read_text(encoding="utf-8"), ns)
    return list(ns["UNIVERSE"])


def _history(db_path: str, table: str, code: str, upto: str, n: int = 25) -> list[dict]:
    with store.conn(db_path) as c:
        c.row_factory = __import__("sqlite3").Row
        rows = c.execute(
            f"SELECT * FROM {table} WHERE code=? AND data_date<=? ORDER BY data_date DESC LIMIT ?",
            (code, upto, n),
        ).fetchall()
    return [dict(r) for r in rows]


def _snapshot_rows(db_path: str, code: str, T: str) -> list[dict]:
    with store.conn(db_path) as c:
        c.row_factory = __import__("sqlite3").Row
        rows = c.execute(
            "SELECT * FROM b_snapshot WHERE code=? AND data_date=? ORDER BY slot", (code, T),
        ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    print(run())
