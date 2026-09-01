"""Read-only bridge from an existing MLS SQLite database.

The bridge intentionally has no guessed table names. Integration later is
performed by supplying explicit SELECT queries whose result columns match the
plugin contract. This prevents silent fallback to stale or semantically wrong
fields.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


class RequiredColumnsError(ValueError):
    pass


def open_source_readonly(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_mapped_rows(conn: sqlite3.Connection, query: str, *, required: set[str], params: Iterable = ()):
    cur = conn.execute(query, tuple(params))
    cols = {d[0] for d in cur.description or []}
    missing = required - cols
    if missing:
        raise RequiredColumnsError(f"source query missing required columns: {sorted(missing)}")
    return cur.fetchall()


def source_capabilities(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY name").fetchall()
    return {"objects": [dict(r) for r in rows]}
