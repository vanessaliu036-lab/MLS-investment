"""Line B Watch Ledger - presentation layer, read-only."""
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
              "Not a forward guarantee - this ledger exists to track whether it holds up."),
}


def _row_dict(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


_STATUS_ORDER = {"CONFIRMED": 0, "WATCH_CLOSELY": 1, "WAIT": 2, "GIVE_UP": 3}


def _empty_context(data_date: Optional[str] = None) -> dict:
    """Return a renderable empty state before the append-only table is created."""
    return dict(
        data_date=data_date,
        has_data=False,
        c1_c2_list=[],
        flow_confirmed_top3=[],
        intraday_discovery=[],
        labels=HISTORICAL_LABELS,
        is_live=False,
    )


def _sort_key(r: dict):
    e = r["explain"]
    dist = e.get("distance_pct")
    return (_STATUS_ORDER.get(e["status"], 9), -(r.get("flow_confirm_magnitude") or 0),
            -(dist if dist is not None else -999))


def _finalize(rows: list[dict], data_date: Optional[str]) -> dict:
    """EOD and live paths share the same sorting and buckets."""
    c1_c2_list = [r for r in rows if r["source"] == "C1C2_PASS"]
    intraday_discovery = [r for r in rows if r["source"] == "INTRADAY_DISCOVERY"]

    c1_c2_list.sort(key=_sort_key)
    intraday_discovery.sort(key=lambda r: -(r.get("flow_confirm_magnitude") or 0))

    confirmed = [r for r in c1_c2_list if r.get("flow_class") in ("OPEN_POSITIVE", "FLOW_FLIP")]
    flow_confirmed_top3 = sorted(
        confirmed, key=lambda r: (r.get("flow_confirm_magnitude") or 0), reverse=True,
    )[:3]

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
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("line_b_watch_ledger",),
        ).fetchone()
        if table is None:
            return _empty_context(data_date)
        if data_date is None:
            row = conn.execute("SELECT MAX(data_date) FROM line_b_watch_ledger").fetchone()
            data_date = row[0] if row else None
        if data_date is None:
            return _empty_context()
        rows = _row_dict(conn, "SELECT * FROM line_b_watch_ledger WHERE data_date=? ORDER BY code",
                         (data_date,))
    finally:
        conn.close()

    for r in rows:
        r["explain"] = _explain.explain(r, is_eod=True)
    ctx = _finalize(rows, data_date)
    ctx["is_live"] = False
    return ctx


def build_live_context(db_path: str = "mls.db", T: Optional[str] = None) -> dict:
    """Build the read-only intraday view and reuse the same presentation order."""
    import line_b_live as _live

    result = _live.build_live_rows(db_path, T)
    ctx = _finalize(result["rows"], result["T"])
    ctx["is_live"] = True
    ctx["t1_used"] = result.get("T1")
    ctx["feed_diag"] = result.get("feed_diag")
    return ctx
