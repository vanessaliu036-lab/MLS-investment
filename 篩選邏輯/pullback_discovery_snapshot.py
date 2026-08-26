"""Append-only ledger for the Healthy Pullback Entry Framework(DISCOVERY ONLY)。

規格與判定見 winning_model_backtest/FROZEN_HEALTHY_PULLBACK_V1.md。
這張表純觀察:不進 UI、不影響 ENTER/WAIT、不影響排序。同一 (code,d1_date)
只寫一次，歷史列不可回寫覆蓋(跟 early_activation_snapshot.py 同一套防呆)。
"""
from __future__ import annotations

import datetime as dt
import hashlib

import store

TABLE = "pullback_discovery"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    code TEXT NOT NULL,
    d1_date TEXT NOT NULL,
    limitup_date TEXT NOT NULL,
    limitup_close REAL,
    d1_close REAL,
    d2_date TEXT,
    d2_close REAL,
    classification TEXT NOT NULL,
    n_slots INTEGER,
    peak_idx INTEGER,
    peak_price REAL,
    trough_idx INTEGER,
    trough_price REAL,
    pullback_depth REAL,
    flow_retention REAL,
    flow_possibly_stale INTEGER,
    impulse_rate_per_min REAL,
    pullback_rate_per_min REAL,
    volume_contraction REAL,
    support_hold INTEGER,
    reclaim_idx INTEGER,
    entry_price REAL,
    mfe_h15m REAL, mae_h15m REAL, net_h15m REAL,
    mfe_h30m REAL, mae_h30m REAL, net_h30m REAL,
    mfe_h60m REAL, mae_h60m REAL, net_h60m REAL,
    mfe_close REAL, mae_close REAL, net_close REAL,
    d2_fwd_ret REAL,
    rule_version TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (code, d1_date)
);
"""


class SnapshotMutationRefused(RuntimeError):
    """同一 (code,d1_date) 的凍結事實跟已存在的列不一樣 —— 拒寫，不覆蓋歷史觀察。"""


# 只對「凍結公式算出來、當天就固定」的欄位取雜湊；d2_fwd_ret 之後才補齊，
# 不算進雜湊，否則 backfill 那次 UPDATE 會被誤判成竄改歷史。
_HASH_KEYS = (
    "code", "d1_date", "limitup_date", "limitup_close", "classification",
    "n_slots", "peak_idx", "peak_price", "trough_idx", "trough_price",
    "pullback_depth", "flow_retention", "volume_contraction", "support_hold",
    "reclaim_idx", "entry_price", "rule_version",
)


def ensure(db_path: str = "mls.db") -> None:
    with store.conn(db_path) as conn:
        conn.executescript(DDL)
        conn.commit()


def _semantic_hash(row: dict) -> str:
    canonical = "\n".join(f"{key}={row.get(key)!r}" for key in _HASH_KEYS)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_cases(cases: list[dict], db_path: str = "mls.db") -> int:
    """append-only 寫入。已存在的 (code,d1_date) 若凍結欄位相同就跳過，
    不同就拒寫(SnapshotMutationRefused)——凍結公式算出來的東西不該變。
    """
    ensure(db_path)
    created_at = dt.datetime.now().isoformat(timespec="seconds")
    columns = [c for c in DDL.split("(", 1)[1].split(")")[0].split(",")]
    columns = [c.strip().split()[0] for c in columns if c.strip() and "PRIMARY KEY" not in c]

    prepared = []
    for case in cases:
        row = {col: case.get(col) for col in columns}
        row["created_at"] = created_at
        row["support_hold"] = (int(row["support_hold"]) if row.get("support_hold") is not None else None)
        row["flow_possibly_stale"] = (int(row["flow_possibly_stale"]) if row.get("flow_possibly_stale") is not None else None)
        row["snapshot_hash"] = _semantic_hash(row)
        prepared.append(row)
    if not prepared:
        return 0

    with store.conn(db_path) as conn:
        rows = conn.execute(f"SELECT code,d1_date,snapshot_hash FROM {TABLE}").fetchall()
        existing = {(r[0], r[1]): r[2] for r in rows}

        changed = [(row["code"], row["d1_date"]) for row in prepared
                   if (row["code"], row["d1_date"]) in existing
                   and existing[(row["code"], row["d1_date"])] != row["snapshot_hash"]]
        if changed:
            raise SnapshotMutationRefused(
                f"pullback_discovery 凍結欄位跟既有紀錄不同,拒寫: {changed[:5]}")

        to_insert = [row for row in prepared if (row["code"], row["d1_date"]) not in existing]
        if not to_insert:
            return 0
        cols = list(to_insert[0])
        ph = ",".join("?" for _ in cols)
        cursor = conn.executemany(
            f"INSERT INTO {TABLE} ({','.join(cols)}) VALUES ({ph})",
            [tuple(row[c] for c in cols) for row in to_insert])
        conn.commit()
    return cursor.rowcount


def backfill_d2(db_path: str = "mls.db") -> int:
    """D2 收盤價出來後才補 d2_fwd_ret；不算進 snapshot_hash,不算竄改。"""
    ensure(db_path)
    updated = 0
    with store.conn(db_path) as conn:
        pending = conn.execute(
            f"SELECT code,d1_date,d1_close FROM {TABLE} WHERE d2_fwd_ret IS NULL"
        ).fetchall()
        for code, d1_date, d1_close in pending:
            if not d1_close:
                continue
            nxt = conn.execute(
                "SELECT data_date,close FROM daily_bar WHERE code=? AND data_date>? "
                "ORDER BY data_date LIMIT 1", (code, d1_date)).fetchone()
            if not nxt or nxt[1] is None:
                continue
            d2_date, d2_close = nxt[0], float(nxt[1])
            d2_fwd_ret = d2_close / float(d1_close) - 1
            conn.execute(
                f"UPDATE {TABLE} SET d2_date=?,d2_close=?,d2_fwd_ret=? "
                "WHERE code=? AND d1_date=?",
                (d2_date, d2_close, d2_fwd_ret, code, d1_date))
            updated += 1
        conn.commit()
    return updated
