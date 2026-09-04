"""Line B Watch Ledger - presentation layer, read-only."""
from __future__ import annotations

import sqlite3
from typing import Optional

import line_b_explain as _explain
import line_b_monitor as _monitor

HISTORICAL_LABELS = {
    "c1_c2_rate": "64.1%",
    "flow_confirmed_label": "A-flow 確認後累積命中率",
    "flow_confirmed_rate": "資料累積中",
    "flow_no_flip_rate": "2.8%",
    "sample_note": "11 clean days · n=561 · day-equal · 2026-08-26 One-Shot Acceptance",
    "flow_confirmed_sample_note": "等待逐日 ledger 累積",
    "caveat": ("Retrospective result on the available clean-day sample at freeze time. "
              "Not a forward guarantee - this ledger exists to track whether it holds up."),
}


def _page_labels(db_path: str = "mls.db") -> dict:
    """Build page labels from the expanding, persisted C2+A-flow cohort."""
    labels = dict(HISTORICAL_LABELS)
    try:
        import line_b_verdict as _verdict
        result = _verdict.cumulative_confirmed_rates(db_path)
    except Exception:
        return labels

    if result["n"]:
        labels["flow_confirmed_rate"] = f'{result["hit_rate_pct"]:.1f}%'
        labels["flow_confirmed_sample_note"] = (
            f'累積 {result["n_days"]} 個交易日 · n={result["n"]} · '
            f'{result["data_dates"][0]}～{result["data_dates"][-1]}'
        )
        labels["flow_confirmed_hint"] = (
            f'命中 {result["hit_count"]} · 未命中 {result["no_hit_count"]} · '
            "新交易日加入後只會累加，不捨棄舊樣本"
        )
    return labels


def _row_dict(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


_STATUS_ORDER = {"PRICE_TRIGGERED": 0, "CONFIRMED": 1, "WATCH_CLOSELY": 2,
                 "WAIT": 3, "GIVE_UP": 4}


def _empty_context(data_date: Optional[str] = None) -> dict:
    """Return a renderable empty state before the append-only table is created."""
    return dict(
        data_date=data_date,
        has_data=False,
        c1_c2_list=[],
        monitoring_list=[],
        monitor_sections=[],
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


def _finalize(rows: list[dict], data_date: Optional[str], labels: Optional[dict] = None,
              is_eod: bool = False) -> dict:
    """EOD and live paths share the same sorting and buckets."""
    for r in rows:
        r["monitor_bucket"] = _monitor.classify(r, is_eod=is_eod)

    c1_c2_list = [r for r in rows if r["source"] == "C1C2_PASS"]
    intraday_discovery = [r for r in rows if r["source"] == "INTRADAY_DISCOVERY"]

    c1_c2_list.sort(key=_sort_key)
    intraday_discovery.sort(key=lambda r: -(r.get("flow_confirm_magnitude") or 0))

    monitoring_list = [r for r in rows if r.get("monitor_bucket")]
    monitoring_list.sort(key=lambda r: (
        _monitor.BUCKET_ORDER.get(r["monitor_bucket"], 9),
        -(r.get("flow_confirm_magnitude") or 0),
        -((r.get("explain") or {}).get("distance_pct") or -999),
    ))
    confirmed = [r for r in monitoring_list
                 if r.get("monitor_bucket") in ("PRICE_TRIGGERED", "CONFIRMED")]
    flow_confirmed_top3 = sorted(
        confirmed, key=lambda r: (r.get("flow_confirm_magnitude") or 0), reverse=True,
    )[:3]
    monitor_sections = [
        {"bucket": bucket, "label": _monitor.BUCKET_LABELS[bucket],
         "rows": [r for r in monitoring_list if r.get("monitor_bucket") == bucket]}
        for bucket in _monitor.BUCKET_ORDER
        if any(r.get("monitor_bucket") == bucket for r in monitoring_list)
    ]

    counts = dict(c1_c2=len(c1_c2_list), confirmed=len(confirmed),
                  discovery=len(intraday_discovery), monitor=len(monitoring_list))
    counts.update({bucket.lower(): sum(1 for r in monitoring_list
                                       if r.get("monitor_bucket") == bucket)
                   for bucket in _monitor.BUCKET_ORDER})

    return dict(
        data_date=data_date, has_data=len(rows) > 0,
        c1_c2_list=c1_c2_list,
        monitoring_list=monitoring_list,
        monitor_sections=monitor_sections,
        flow_confirmed_top3=flow_confirmed_top3,
        intraday_discovery=intraday_discovery,
        counts=counts,
        labels=labels or HISTORICAL_LABELS,
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
    ctx = _finalize(rows, data_date, _page_labels(db_path), is_eod=True)
    ctx["is_live"] = False
    return ctx


def build_live_context(db_path: str = "mls.db", T: Optional[str] = None) -> dict:
    """Build the read-only intraday view and reuse the same presentation order."""
    import line_b_live as _live

    result = _live.build_live_rows(db_path, T)
    ctx = _finalize(result["rows"], result["T"], _page_labels(db_path), is_eod=False)
    ctx["is_live"] = True
    ctx["t1_used"] = result.get("T1")
    ctx["feed_diag"] = result.get("feed_diag")
    return ctx
