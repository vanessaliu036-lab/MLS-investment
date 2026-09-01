"""SQLite repository for the isolated preview plugin."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def open_db(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = str(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def apply_schema(conn: sqlite3.Connection, schema_path: str | Path) -> None:
    conn.executescript(Path(schema_path).read_text(encoding="utf-8"))
    conn.commit()


def current_top_rows(conn: sqlite3.Connection, trade_date: str, direction: str, limit: int = 20):
    order = "DESC" if direction == "inflow" else "ASC"
    sign = "> 0" if direction == "inflow" else "< 0"
    return conn.execute(
        f"""WITH latest AS (
                SELECT symbol, MAX(ts) ts
                FROM intraday_snapshot
                WHERE trade_date=?
                GROUP BY symbol
            )
            SELECT s.*
            FROM intraday_snapshot s
            JOIN latest l ON l.symbol=s.symbol AND l.ts=s.ts
            WHERE s.trade_date=? AND s.net_flow_amount {sign}
            ORDER BY s.net_flow_amount {order}
            LIMIT ?""",
        (trade_date, trade_date, limit),
    ).fetchall()


def threshold_for_symbol(conn: sqlite3.Connection, symbol: str) -> tuple[float | None, int]:
    for key in (f"symbol:{symbol}", "default"):
        row = conn.execute(
            "SELECT min_amount_threshold, window_ticks_required FROM flow_threshold_config WHERE threshold_key=?",
            (key,),
        ).fetchone()
        if row:
            return float(row["min_amount_threshold"]), int(row["window_ticks_required"])
    return None, 2


def consecutive_flow_ticks(conn: sqlite3.Connection, symbol: str, trade_date: str, threshold: float | None) -> int:
    if threshold is None or threshold <= 0:
        return 0
    rows = conn.execute(
        """SELECT net_flow_amount FROM intraday_snapshot
           WHERE symbol=? AND trade_date=? ORDER BY ts DESC LIMIT 30""",
        (symbol, trade_date),
    ).fetchall()
    if not rows or rows[0]["net_flow_amount"] is None:
        return 0
    first = float(rows[0]["net_flow_amount"])
    if abs(first) < threshold or first == 0:
        return 0
    direction = 1 if first > 0 else -1
    n = 0
    for row in rows:
        v = row["net_flow_amount"]
        if v is None or abs(float(v)) < threshold or (1 if float(v) > 0 else -1) != direction:
            break
        n += 1
    return n


def aflow_positive_two_samples(conn: sqlite3.Connection, symbol: str, trade_date: str) -> bool:
    rows = conn.execute(
        """SELECT a_flow FROM intraday_snapshot
           WHERE symbol=? AND trade_date=? ORDER BY ts DESC LIMIT 2""",
        (symbol, trade_date),
    ).fetchall()
    return len(rows) == 2 and all(r["a_flow"] is not None and float(r["a_flow"]) > 0 for r in rows)


def chip_4d_summary(conn: sqlite3.Connection, symbol: str, trade_date: str) -> dict:
    rows = conn.execute(
        """SELECT * FROM chip_daily
           WHERE symbol=? AND trade_date < ?
           ORDER BY trade_date DESC LIMIT 4""",
        (symbol, trade_date),
    ).fetchall()
    if not rows:
        return {
            "foreign_net_4d": None,
            "institutional_net_4d": None,
            "volume_4d": None,
            "big_holder_trend": None,
            "chip_data_date": None,
        }
    return {
        "foreign_net_4d": sum(float(r["foreign_net_lots"] or 0) for r in rows),
        "institutional_net_4d": sum(float(r["institutional_net_lots"] or 0) for r in rows),
        "volume_4d": sum(float(r["volume_lots"] or 0) for r in rows),
        "big_holder_trend": rows[0]["big_holder_trend"],
        "chip_data_date": rows[0]["chip_data_date"] or rows[0]["trade_date"],
    }


def latest_trigger_context(conn: sqlite3.Connection, symbol: str, trade_date: str) -> dict:
    row = conn.execute(
        """SELECT * FROM trigger_context
           WHERE symbol=? AND trade_date <= ?
           ORDER BY trade_date DESC LIMIT 1""",
        (symbol, trade_date),
    ).fetchone()
    if not row:
        return {"trigger_failed": None, "trigger_passed": None, "trigger_date": None}
    return {
        "trigger_failed": None if row["trigger_failed"] is None else bool(row["trigger_failed"]),
        "trigger_passed": None if row["trigger_passed"] is None else bool(row["trigger_passed"]),
        "trigger_date": row["trade_date"],
        "trigger_price": row["trigger_price"],
        "monitor_price": row["monitor_price"],
        "source_data_date": row["source_data_date"],
    }


def market_regime(conn: sqlite3.Connection, trade_date: str) -> dict:
    row = conn.execute("SELECT * FROM market_regime_daily WHERE trade_date=?", (trade_date,)).fetchone()
    if not row:
        return {"regime": "UNKNOWN", "baseline_up_rate": None}
    return {"regime": row["regime"] or "UNKNOWN", "baseline_up_rate": row["baseline_up_rate"]}


def expected_chip_date(conn: sqlite3.Connection, trade_date: str) -> str | None:
    row = conn.execute("SELECT MAX(trade_date) d FROM chip_daily WHERE trade_date < ?", (trade_date,)).fetchone()
    return row["d"] if row else None


def current_latest_rows(conn: sqlite3.Connection, trade_date: str):
    """Latest snapshot for every symbol on the requested trade date."""
    return conn.execute(
        """WITH latest AS (
                SELECT symbol, MAX(ts) ts
                FROM intraday_snapshot
                WHERE trade_date=?
                GROUP BY symbol
            )
            SELECT s.*
            FROM intraday_snapshot s
            JOIN latest l ON l.symbol=s.symbol AND l.ts=s.ts
            WHERE s.trade_date=?""",
        (trade_date, trade_date),
    ).fetchall()


def institutional_outflow_summary(conn: sqlite3.Connection, symbol: str, trade_date: str) -> dict:
    """Prior institutional flow for the reversal research track.

    Returns both signed net sums and standardized ratios. Ratios are only
    emitted when the full requested window exists and traded volume is valid.
    """
    rows = conn.execute(
        """SELECT * FROM chip_daily
           WHERE symbol=? AND trade_date < ?
           ORDER BY trade_date DESC LIMIT 20""",
        (symbol, trade_date),
    ).fetchall()

    def window_net(n: int):
        if len(rows) < n:
            return None
        return sum(float(r["institutional_net_lots"] or 0) for r in rows[:n])

    def window_ratio(n: int):
        if len(rows) < n:
            return None
        sub = rows[:n]
        volume = sum(float(r["volume_lots"] or 0) for r in sub)
        if volume <= 0:
            return None
        net = sum(float(r["institutional_net_lots"] or 0) for r in sub)
        return net / volume

    return {
        "institutional_net_1d": window_net(1),
        "institutional_net_5d": window_net(5),
        "institutional_net_20d": window_net(20),
        "institutional_net_5d_ratio": window_ratio(5),
        "institutional_net_20d_ratio": window_ratio(20),
        "n_days": len(rows),
        "chip_data_date": (rows[0]["chip_data_date"] or rows[0]["trade_date"]) if rows else None,
    }


def _snapshot_dt(trade_date: str, ts: str):
    from datetime import datetime
    text = str(ts)
    if "T" in text:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    return datetime.fromisoformat(f"{trade_date}T{text}")


def aflow_persistence_metrics(
    conn: sqlite3.Connection,
    symbol: str,
    trade_date: str,
    *,
    min_minutes: int = 30,
    max_minutes: int = 90,
) -> dict:
    """Compare latest snapshot with the nearest sample 30-90 minutes earlier."""
    rows = conn.execute(
        """SELECT ts, close, a_flow, volume, vwap
           FROM intraday_snapshot
           WHERE symbol=? AND trade_date=?
           ORDER BY ts""",
        (symbol, trade_date),
    ).fetchall()
    if not rows:
        return {
            "reference_ts": None, "elapsed_minutes": None,
            "aflow_delta": None, "price_delta": None,
            "aflow_persistence": False, "price_confirmation": False,
        }
    latest = rows[-1]
    latest_dt = _snapshot_dt(trade_date, latest["ts"])
    candidates = []
    for row in rows[:-1]:
        dt = _snapshot_dt(trade_date, row["ts"])
        elapsed = (latest_dt - dt).total_seconds() / 60
        if min_minutes <= elapsed <= max_minutes:
            candidates.append((dt, elapsed, row))
    if not candidates:
        return {
            "reference_ts": None, "elapsed_minutes": None,
            "aflow_delta": None, "price_delta": None,
            "aflow_persistence": False, "price_confirmation": False,
        }
    _, elapsed, ref = max(candidates, key=lambda x: x[0])
    la, ra = latest["a_flow"], ref["a_flow"]
    lc, rc = latest["close"], ref["close"]
    aflow_delta = (float(la) - float(ra)) if la is not None and ra is not None else None
    price_delta = (float(lc) - float(rc)) if lc is not None and rc is not None else None
    aflow_persistence = bool(
        la is not None and ra is not None and float(ra) > 0 and float(la) > float(ra)
    )
    price_confirmation = bool(price_delta is not None and price_delta > 0)
    return {
        "reference_ts": ref["ts"],
        "elapsed_minutes": round(elapsed, 1),
        "aflow_delta": aflow_delta,
        "price_delta": price_delta,
        "aflow_persistence": aflow_persistence,
        "price_confirmation": price_confirmation,
    }
