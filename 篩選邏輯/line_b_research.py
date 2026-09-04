"""Line B C2 + A-flow forward research collector.

This module is deliberately separate from the decision path.  It records a
frozen C2 + A-flow confirmation event after the session, then fills forward
outcomes from already persisted ``b_snapshot`` and ``daily_bar`` rows.

No order is sent and no existing Line B field is rewritten.  The event table
is research-only; missing future data remains NULL until the next scheduled
run has an immediate next-trading-day bar available.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from collections import defaultdict
from typing import Optional

from phase import next_trading_day, today_tw

TABLE = "line_b_research"
BLIND_MIN_SLOT = "0915"
FLOW_CONFIRMED = {"OPEN_POSITIVE", "FLOW_FLIP"}
SWING_COST_BPS = 47.1
DAYTRADE_COST_BPS = 32.1
INTRADAY_HORIZONS = (15, 30, 60)
VERSION = "line_b_c2_aflow_research_v1_2026-09-03"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    data_date TEXT NOT NULL,
    code TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    source TEXT NOT NULL,
    c1_pass INTEGER,
    c2_pass INTEGER,
    flow_class TEXT NOT NULL,
    flow_confirm_magnitude REAL,
    confirmation_slot TEXT NOT NULL,
    confirmation_price REAL NOT NULL,
    t1_close REAL,
    t1_ma20 REAL,
    t1_prior_high REAL,
    market_regime TEXT,
    market_regime_raw TEXT,
    market_breadth_pct REAL,
    market_context_source TEXT,
    round_trip_cost_bps REAL NOT NULL,
    intraday_round_trip_cost_bps REAL NOT NULL,
    execution_lag_bps REAL,
    actual_fill_price REAL,
    actual_slippage_bps REAL,
    slippage_status TEXT NOT NULL,
    mfe_15m_gross_pct REAL,
    mae_15m_gross_pct REAL,
    mfe_15m_net_pct REAL,
    mae_15m_net_pct REAL,
    mfe_30m_gross_pct REAL,
    mae_30m_gross_pct REAL,
    mfe_30m_net_pct REAL,
    mae_30m_net_pct REAL,
    mfe_60m_gross_pct REAL,
    mae_60m_gross_pct REAL,
    mfe_60m_net_pct REAL,
    mae_60m_net_pct REAL,
    mfe_to_close_gross_pct REAL,
    mae_to_close_gross_pct REAL,
    mfe_to_close_net_pct REAL,
    mae_to_close_net_pct REAL,
    t1_matured_date TEXT,
    t1_open_price REAL,
    t1_close_price REAL,
    t1_return_gross_pct REAL,
    t1_return_net_pct REAL,
    t1_win INTEGER,
    t1_mfe_gross_pct REAL,
    t1_mae_gross_pct REAL,
    t1_mfe_net_pct REAL,
    t1_mae_net_pct REAL,
    t1_mfe_3pct_hit INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (data_date, code)
);
CREATE INDEX IF NOT EXISTS idx_line_b_research_regime
    ON {TABLE}(market_regime, t1_matured_date);
CREATE INDEX IF NOT EXISTS idx_line_b_research_flow
    ON {TABLE}(flow_class, t1_matured_date);
"""


def ensure(db_path: str = "mls.db") -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(DDL)
        conn.commit()


def _slot_minutes(slot: str) -> int:
    value = str(slot or "")
    if len(value) != 4 or not value.isdigit():
        return -1
    return int(value[:2]) * 60 + int(value[2:])


def _usable_snapshots(snapshots: list[dict]) -> list[dict]:
    return sorted(
        [
            r for r in snapshots
            if str(r.get("slot", "0000")) >= BLIND_MIN_SLOT
            and r.get("price") is not None
            and r.get("net_active") is not None
        ],
        key=lambda r: str(r.get("slot", "0000")),
    )


def build_event(ledger_row: dict, snapshots: list[dict],
                market_context: Optional[dict] = None) -> Optional[dict]:
    """Build one causal C2 + A-flow event, or ``None`` if it does not qualify."""
    if ledger_row.get("source") != "C1C2_PASS":
        return None
    if ledger_row.get("c2_selling_weak_price_resp") not in (1, True):
        return None
    flow_class = ledger_row.get("flow_class")
    if flow_class not in FLOW_CONFIRMED:
        return None

    rows = _usable_snapshots(snapshots)
    confirmation = None
    if flow_class == "OPEN_POSITIVE":
        if rows and float(rows[0]["net_active"]) > 0:
            confirmation = rows[0]
    else:
        for left, right in zip(rows, rows[1:]):
            if float(left["net_active"]) > 0 and float(right["net_active"]) > 0:
                confirmation = right
                break
    if confirmation is None:
        return None

    context = market_context or {}
    return {
        "data_date": ledger_row["data_date"],
        "code": ledger_row["code"],
        "definition_version": VERSION,
        "source": ledger_row["source"],
        "c1_pass": ledger_row.get("c1_structure_intact"),
        "c2_pass": ledger_row.get("c2_selling_weak_price_resp"),
        "flow_class": flow_class,
        "flow_confirm_magnitude": ledger_row.get("flow_confirm_magnitude"),
        "confirmation_slot": str(confirmation["slot"]),
        "confirmation_price": float(confirmation["price"]),
        "t1_close": ledger_row.get("t1_close"),
        "t1_ma20": ledger_row.get("t1_ma20"),
        "t1_prior_high": ledger_row.get("t1_prior_high"),
        "market_regime": context.get("market_regime"),
        "market_regime_raw": context.get("market_regime_raw"),
        "market_breadth_pct": context.get("market_breadth_pct"),
        "market_context_source": context.get("market_context_source"),
        "round_trip_cost_bps": SWING_COST_BPS,
        "intraday_round_trip_cost_bps": DAYTRADE_COST_BPS,
        "actual_fill_price": None,
        "actual_slippage_bps": None,
        "slippage_status": "NOT_AVAILABLE_NO_ORDER_FILL",
    }


def _pct(numerator: float, denominator: float) -> float:
    return (numerator / denominator - 1.0) * 100.0


def _round(value: Optional[float], places: int = 3) -> Optional[float]:
    return round(value, places) if value is not None else None


def intraday_outcomes(event: dict, snapshots: list[dict],
                      cost_bps: float = DAYTRADE_COST_BPS) -> dict:
    """Measure forward-only same-day MFE/MAE after the confirmation slot."""
    entry = float(event["confirmation_price"])
    confirm_min = _slot_minutes(event["confirmation_slot"])
    rows = [
        r for r in _usable_snapshots(snapshots)
        if _slot_minutes(str(r["slot"])) > confirm_min
    ]
    cost_pct = cost_bps / 100.0
    out: dict = {}
    next_price = rows[0].get("price") if rows else None
    out["execution_lag_bps"] = (
        _round((_pct(float(next_price), entry)) * 100.0, 3)
        if next_price is not None else None
    )

    def add(prefix: str, window: list[dict]) -> None:
        if not window:
            for suffix in ("gross_pct", "net_pct"):
                out[f"mfe_{prefix}_{suffix}"] = None
                out[f"mae_{prefix}_{suffix}"] = None
            return
        highs = [float(r["price"]) for r in window]
        lows = [float(r["price"]) for r in window]
        mfe = _pct(max(highs), entry)
        mae = _pct(min(lows), entry)
        out[f"mfe_{prefix}_gross_pct"] = _round(mfe)
        out[f"mae_{prefix}_gross_pct"] = _round(mae)
        out[f"mfe_{prefix}_net_pct"] = _round(mfe - cost_pct)
        out[f"mae_{prefix}_net_pct"] = _round(mae - cost_pct)

    for horizon in INTRADAY_HORIZONS:
        add(f"{horizon}m", [
            r for r in rows
            if _slot_minutes(str(r["slot"])) <= confirm_min + horizon
        ])
    add("to_close", rows)
    return out


def insert_event(ledger_row: dict, event: dict, db_path: str = "mls.db") -> int:
    """Insert a research event once; later calls cannot rewrite its identity."""
    ensure(db_path)
    now = _dt.datetime.now().isoformat(timespec="seconds")
    row = {
        "data_date": ledger_row["data_date"],
        "code": ledger_row["code"],
        "definition_version": VERSION,
        "source": ledger_row.get("source", "C1C2_PASS"),
        "c1_pass": ledger_row.get("c1_structure_intact"),
        "c2_pass": ledger_row.get("c2_selling_weak_price_resp"),
        "t1_close": ledger_row.get("t1_close"),
        "t1_ma20": ledger_row.get("t1_ma20"),
        "t1_prior_high": ledger_row.get("t1_prior_high"),
        "round_trip_cost_bps": SWING_COST_BPS,
        "intraday_round_trip_cost_bps": DAYTRADE_COST_BPS,
        "actual_fill_price": None,
        "actual_slippage_bps": None,
        "slippage_status": "NOT_AVAILABLE_NO_ORDER_FILL",
    }
    row.update(event)
    row.setdefault("created_at", now)
    row.setdefault("updated_at", now)
    columns = list(row)
    placeholders = ",".join("?" for _ in columns)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {TABLE} ({','.join(columns)}) VALUES ({placeholders})",
            [row[c] for c in columns],
        )
        conn.commit()
        return cur.rowcount


def _t1_values(event: dict, bar: sqlite3.Row) -> dict:
    entry = float(event["confirmation_price"])
    cost_pct = float(event["round_trip_cost_bps"]) / 100.0
    close = bar["close"]
    if close is None:
        return {}
    high = bar["high"]
    low = bar["low"]
    gross_return = _pct(float(close), entry)
    values = {
        "t1_matured_date": bar["data_date"],
        "t1_open_price": bar["open"],
        "t1_close_price": close,
        "t1_return_gross_pct": _round(gross_return),
        "t1_return_net_pct": _round(gross_return - cost_pct),
        "t1_win": int(gross_return - cost_pct > 0),
    }
    if high is not None:
        mfe = _pct(float(high), entry)
        values.update(t1_mfe_gross_pct=_round(mfe), t1_mfe_net_pct=_round(mfe - cost_pct),
                      t1_mfe_3pct_hit=int(mfe - cost_pct >= 3.0))
    if low is not None:
        mae = _pct(float(low), entry)
        values.update(t1_mae_gross_pct=_round(mae), t1_mae_net_pct=_round(mae - cost_pct))
    return values


def backfill_t1(db_path: str = "mls.db", data_date: Optional[str] = None) -> int:
    """Backfill only the immediate next trading day; never skip a missing day."""
    ensure(db_path)
    where = "WHERE t1_matured_date IS NULL"
    params: tuple = ()
    if data_date:
        where += " AND data_date=?"
        params = (data_date,)
    updated = 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        has_daily_bar = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_bar'"
        ).fetchone()
        if has_daily_bar is None:
            return 0
        events = conn.execute(f"SELECT * FROM {TABLE} {where}", params).fetchall()
        for event in events:
            next_date = next_trading_day(_dt.date.fromisoformat(event["data_date"])).isoformat()
            bar = conn.execute(
                "SELECT data_date,open,high,low,close FROM daily_bar WHERE code=? AND data_date=?",
                (event["code"], next_date),
            ).fetchone()
            if bar is None:
                continue
            values = _t1_values(event, bar)
            if not values:
                continue
            values["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            sets = ",".join(f"{k}=?" for k in values)
            conn.execute(
                f"UPDATE {TABLE} SET {sets} WHERE data_date=? AND code=?",
                [*values.values(), event["data_date"], event["code"]],
            )
            updated += 1
        conn.commit()
    return updated


def _market_context() -> dict:
    """Capture the canonical market regime once, with an explicit no-data state."""
    try:
        import market_regime
        breadth = market_regime.fetch_breadth(force=True)
        if not breadth:
            return {"market_regime": "UNKNOWN", "market_regime_raw": "NO_DATA",
                    "market_context_source": "market_regime.fetch_breadth"}
        assessed = market_regime.assess(breadth_row=breadth, index_pct=None)
        return {
            "market_regime": market_regime.normalize_regime(assessed.get("regime")) or "UNKNOWN",
            "market_regime_raw": assessed.get("regime"),
            "market_breadth_pct": round(float(breadth["true_breadth"]) * 100, 1)
            if breadth.get("true_breadth") is not None else None,
            "market_context_source": "market_regime.fetch_breadth",
        }
    except Exception as exc:
        return {"market_regime": "UNKNOWN", "market_regime_raw": type(exc).__name__,
                "market_context_source": "market_regime.unavailable"}


def collect_and_backfill(db_path: str = "mls.db", data_date: Optional[str] = None,
                         market_context: Optional[dict] = None) -> dict:
    """Collect today's C2+A-flow events, same-day forward metrics, and T+1 results."""
    ensure(db_path)
    d = data_date or today_tw().isoformat()
    with sqlite3.connect(db_path) as conn:
        source_tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    if not {"line_b_watch_ledger", "b_snapshot"}.issubset(source_tables):
        # The collector can be installed before the production writer.  Keep
        # the research table healthy and let the next scheduled run catch up.
        return {"data_date": d, "events_created": 0, "events_observed": 0,
                "t1_backfilled": backfill_t1(db_path),
                "market_context": market_context or {"market_regime": "UNKNOWN",
                                                       "market_regime_raw": "NO_DATA"}}
    context = market_context if market_context is not None else _market_context()
    created = 0
    same_day_updated = 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ledgers = conn.execute(
            "SELECT * FROM line_b_watch_ledger WHERE data_date=? AND source='C1C2_PASS' "
            "AND flow_class IN ('OPEN_POSITIVE','FLOW_FLIP')", (d,)
        ).fetchall()
        snapshots = defaultdict(list)
        for raw in conn.execute(
            "SELECT * FROM b_snapshot WHERE data_date=? ORDER BY code,slot", (d,)
        ).fetchall():
            snapshots[raw["code"]].append(dict(raw))

    for raw in ledgers:
        ledger_row = dict(raw)
        event = build_event(ledger_row, snapshots[ledger_row["code"]], context)
        if event is None:
            continue
        event.update(intraday_outcomes(
            event, snapshots[ledger_row["code"]],
            cost_bps=DAYTRADE_COST_BPS,
        ))
        now = _dt.datetime.now().isoformat(timespec="seconds")
        event["created_at"] = now
        event["updated_at"] = now
        columns = list(event)
        placeholders = ",".join("?" for _ in columns)
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO {TABLE} ({','.join(columns)}) VALUES ({placeholders})",
                [event[c] for c in columns],
            )
            conn.commit()
            created += cur.rowcount
        same_day_updated += 1

    matured = backfill_t1(db_path)
    return {"data_date": d, "events_created": created,
            "events_observed": same_day_updated, "t1_backfilled": matured,
            "market_context": context}


def _aggregate(rows: list[sqlite3.Row]) -> dict:
    def average(name: str) -> Optional[float]:
        values = [r[name] for r in rows if r[name] is not None]
        return round(sum(values) / len(values), 3) if values else None

    wins = [r["t1_win"] for r in rows if r["t1_win"] is not None]
    return {
        "n": len(rows),
        "t1_win_rate_pct": round(sum(wins) / len(wins) * 100, 2) if wins else None,
        "avg_t1_return_net_pct": average("t1_return_net_pct"),
        "avg_t1_mfe_net_pct": average("t1_mfe_net_pct"),
        "avg_t1_mae_net_pct": average("t1_mae_net_pct"),
        "avg_mfe_15m_net_pct": average("mfe_15m_net_pct"),
        "avg_mfe_30m_net_pct": average("mfe_30m_net_pct"),
        "avg_mfe_60m_net_pct": average("mfe_60m_net_pct"),
        "avg_mfe_to_close_net_pct": average("mfe_to_close_net_pct"),
        "avg_execution_lag_bps": average("execution_lag_bps"),
        "round_trip_cost_bps": average("round_trip_cost_bps"),
        "intraday_round_trip_cost_bps": average("intraday_round_trip_cost_bps"),
    }


def summary(db_path: str = "mls.db", since: Optional[str] = None) -> dict:
    """Return matured forward metrics overall and split by market regime."""
    ensure(db_path)
    where = "WHERE t1_matured_date IS NOT NULL"
    params: tuple = ()
    if since:
        where += " AND data_date>=?"
        params = (since,)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT * FROM {TABLE} {where} ORDER BY data_date,code", params).fetchall()
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[row["market_regime"] or "UNKNOWN"].append(row)
    return {
        "status": "DESCRIPTIVE_ONLY",
        "definition_version": VERSION,
        "total": _aggregate(rows),
        "by_market_regime": {name: _aggregate(group) for name, group in sorted(groups.items())},
    }
