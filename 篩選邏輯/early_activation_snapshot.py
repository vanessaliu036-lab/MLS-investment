"""Append-only ledger for Early Activation Research.

The table stores the complete T0 discovery facts for every stock in the pool,
including no-setup rows needed by the matched baseline.  Outcomes are filled in
later and never participate in classification.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

import early_activation_score as eas
import store

TABLE = "early_activation_snapshot"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    data_date TEXT NOT NULL,
    code TEXT NOT NULL,
    setup_type TEXT,
    sector_context TEXT NOT NULL,
    evidence_status TEXT NOT NULL,
    reasons TEXT,
    foreign_days REAL,
    foreign_net REAL,
    ma5_distance_pct REAL,
    volume_ratio REAL,
    sector_regime TEXT,
    sector_ret_median REAL,
    sector_breadth REAL,
    base_close REAL,
    history_json TEXT,
    t1_close REAL,
    t1_return_pct REAL,
    hit_plus_3 INTEGER,
    rule_version TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (data_date, code)
);
"""


class RetroactiveWriteRefused(RuntimeError):
    """A newer discovery date exists, so an older date cannot be inserted."""


class SnapshotMutationRefused(RuntimeError):
    """The same T0 stock was reclassified from changed historical facts."""


def ensure(db_path: str = "mls.db") -> None:
    with store.conn(db_path) as conn:
        conn.executescript(DDL)
        conn.commit()


_HASH_KEYS = (
    "data_date", "code", "setup_type", "sector_context", "evidence_status",
    "reasons", "foreign_days", "foreign_net", "ma5_distance_pct",
    "volume_ratio", "sector_regime", "sector_ret_median", "sector_breadth",
    "base_close", "history_json", "rule_version",
)


def _semantic_hash(row: dict) -> str:
    canonical = "\n".join(f"{key}={row.get(key)!r}" for key in _HASH_KEYS)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _date_text(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _snapshot_row(data_date, source: dict, created_at: str) -> dict:
    classified = source.get("early_activation") or {}
    if classified.get("evidence_status") not in (None, eas.DISCOVERY_ONLY):
        raise ValueError("Early Activation evidence status must remain DISCOVERY ONLY")
    row = {
        "data_date": _date_text(data_date),
        "code": str(source["code"]),
        "setup_type": classified.get("setup_type"),
        "sector_context": classified.get("sector_context") or eas.sector_context(source),
        "evidence_status": eas.DISCOVERY_ONLY,
        "reasons": " / ".join(classified.get("reasons") or []),
        "foreign_days": source.get("foreign_days"),
        "foreign_net": source.get("foreign_net"),
        "ma5_distance_pct": source.get("ma5_distance_pct"),
        "volume_ratio": source.get("volume_ratio"),
        "sector_regime": source.get("sector_regime"),
        "sector_ret_median": source.get("sector_ret_median"),
        "sector_breadth": source.get("sector_breadth"),
        "base_close": source.get("close", source.get("base_close")),
        "history_json": json.dumps(source.get("early_history") or [],
                                   ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")),
        "t1_close": None,
        "t1_return_pct": None,
        "hit_plus_3": None,
        "rule_version": classified.get("rule_version") or eas.RULE_VERSION,
        "snapshot_hash": None,
        "created_at": created_at,
    }
    row["snapshot_hash"] = _semantic_hash(row)
    return row


def write_snapshot(data_date, rows: list[dict], db_path: str = "mls.db") -> int:
    """Append one full-pool T0 snapshot without mutating prior observations."""
    ensure(db_path)
    date_text = _date_text(data_date)
    with store.conn(db_path) as conn:
        newest = conn.execute(f"SELECT MAX(data_date) FROM {TABLE}").fetchone()[0]
    if newest and date_text < newest:
        raise RetroactiveWriteRefused(
            f"refusing {date_text}: newer Early Activation snapshot {newest} exists")

    created_at = dt.datetime.now().isoformat(timespec="seconds")
    prepared = [_snapshot_row(data_date, row, created_at) for row in rows]
    if not prepared:
        return 0
    with store.conn(db_path) as conn:
        existing = {row[0]: row[1] for row in conn.execute(
            f"SELECT code,snapshot_hash FROM {TABLE} WHERE data_date=?", (date_text,))}
    changed = [row["code"] for row in prepared
               if row["code"] in existing and
               row["snapshot_hash"] != existing[row["code"]]]
    if changed:
        raise SnapshotMutationRefused(
            f"{date_text} Early Activation facts changed for {changed[:5]}; "
            "historical discovery snapshots are immutable")
    prepared = [row for row in prepared if row["code"] not in existing]
    if not prepared:
        return 0

    columns = list(prepared[0])
    placeholders = ",".join("?" for _ in columns)
    with store.conn(db_path) as conn:
        cursor = conn.executemany(
            f"INSERT INTO {TABLE} ({','.join(columns)}) VALUES ({placeholders})",
            [tuple(row[column] for column in columns) for row in prepared])
        conn.commit()
    return cursor.rowcount


def backfill_t1(db_path: str = "mls.db") -> int:
    """Fill T+1 close-to-close discovery outcomes from the next trading bar."""
    ensure(db_path)
    updated = 0
    with store.conn(db_path) as conn:
        pending = conn.execute(
            f"SELECT data_date,code,base_close FROM {TABLE} "
            "WHERE t1_return_pct IS NULL").fetchall()
        for data_date, code, base_close in pending:
            if not base_close:
                continue
            next_bar = conn.execute(
                "SELECT close FROM daily_bar WHERE code=? AND data_date>? "
                "ORDER BY data_date LIMIT 1", (code, data_date)).fetchone()
            if not next_bar or next_bar[0] is None:
                continue
            t1_close = float(next_bar[0])
            outcome = round((t1_close / float(base_close) - 1.0) * 100.0, 3)
            conn.execute(
                f"UPDATE {TABLE} SET t1_close=?,t1_return_pct=?,hit_plus_3=? "
                "WHERE data_date=? AND code=?",
                (t1_close, outcome, int(outcome >= 3.0), data_date, code))
            updated += 1
        conn.commit()
    return updated


def research_summary(db_path: str = "mls.db") -> dict:
    """Read only this ledger and return discovery metrics plus matched baselines."""
    ensure(db_path)
    with store.conn(db_path) as conn:
        rows = [dict(row) for row in conn.execute(
            f"SELECT data_date,code,setup_type,sector_context,t1_return_pct "
            f"FROM {TABLE} WHERE t1_return_pct IS NOT NULL")]
    return eas.evaluate(rows)

