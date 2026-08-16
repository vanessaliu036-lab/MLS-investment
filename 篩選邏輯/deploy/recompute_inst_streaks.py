#!/usr/bin/env python3
"""Repair derived institution streak columns from immutable net values.

The net columns are preserved; only the derived streak columns are rebuilt.
Run from /opt/mls-screen with the AB service stopped and a DB backup present.
"""
import sqlite3

DB = "mls.db"

def streak(values):
    values = list(values)
    if not values or not values[-1]:
        return 0
    positive = values[-1] > 0
    n = 0
    for value in reversed(values):
        if value and (value > 0) == positive:
            n += 1
        else:
            break
    return n if positive else -n

con = sqlite3.connect(DB)
try:
    rows = con.execute(
        "select code,data_date,foreign_net,trust_net,dealer_net,total_net "
        "from inst_flow order by code,data_date"
    ).fetchall()
    by_code = {}
    for row in rows:
        by_code.setdefault(row[0], []).append(row)
    updates = []
    for code, items in by_code.items():
        for index, row in enumerate(items):
            history = items[:index + 1]
            updates.append((
                streak([r[2] or 0 for r in history]),
                streak([r[3] or 0 for r in history]),
                streak([r[4] or 0 for r in history]),
                streak([r[5] or 0 for r in history]),
                code, row[1],
            ))
    con.execute("begin")
    con.execute("drop trigger if exists inst_flow_no_update")
    con.execute("drop trigger if exists inst_flow_no_delete")
    con.executemany(
        "update inst_flow set foreign_days=?,trust_days=?,dealer_days=?,consecutive_days=? "
        "where code=? and data_date=?", updates
    )
    con.execute("create trigger inst_flow_no_update before update on inst_flow begin select raise(abort, 'IMMUTABLE: inst_flow history data is locked'); end")
    con.execute("create trigger inst_flow_no_delete before delete on inst_flow begin select raise(abort, 'IMMUTABLE: inst_flow history data is locked'); end")
    con.commit()
    print(f"recomputed {len(updates)} inst_flow rows")
finally:
    con.close()
