"""Pre-Activation 每日快照與後續追蹤(2026-08-24 起)。

目的:累積「四階段標記後續 3~7 天實際表現」的**前瞻**樣本。
2026 holdout 已在研究端被看過,所以下一個真正乾淨的驗證來源就是
8/24 之後的 live observation —— 這批資料沒有回看偏誤,不能污染。

只寫 stage 與當下事實,不寫任何預測分數。
"""
from __future__ import annotations
import datetime as _dt
import store

TABLE = "pa_snapshot"
PLUGIN = "pre_activation"
COST_PCT = 0.471          # 來回成本,與研究端一致

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    data_date TEXT NOT NULL,          -- 快照日 T0(盤後)
    code TEXT NOT NULL,
    stage TEXT,                       -- EARLY / ARMED / TRIGGER / EXTENDED / —
    foreign_state TEXT, foreign_days REAL,
    volume_state TEXT, volume_ratio REAL,
    ma5_state TEXT, ma5_distance_pct REAL,
    price_state TEXT, breakout_5d_pct REAL,
    do_not_chase INTEGER,
    legacy_rank INTEGER, continuation REAL,
    base_close REAL,                  -- T0 收盤(僅供對照)
    entry_open REAL,                  -- T+1 開盤(實際進場價,隔日回填)
    ret_t1 REAL, ret_t3 REAL, ret_t5 REAL, ret_t7 REAL,
    net_t3 REAL, net_t5 REAL, net_t7 REAL,
    mfe_t7 REAL, mae_t7 REAL,
    rule_version TEXT, created_at TEXT,
    PRIMARY KEY (data_date, code)
);
"""


def ensure(db_path: str = "mls.db") -> None:
    with store.conn(db_path) as c:
        c.executescript(DDL)


def write_snapshot(data_date: _dt.date, rows: list[dict],
                   db_path: str = "mls.db") -> int:
    """rows 需含 code / pre_activation / continuation / legacy_rank / close。"""
    ensure(db_path)
    now = _dt.datetime.now().isoformat(timespec="seconds")
    out = []
    for r in rows:
        pa = r.get("pre_activation") or {}
        if not pa:
            continue
        out.append({
            "data_date": data_date.isoformat(), "code": str(r.get("code")),
            "stage": pa.get("stage"),
            "foreign_state": pa.get("foreign_state"), "foreign_days": pa.get("foreign_days"),
            "volume_state": pa.get("volume_state"), "volume_ratio": pa.get("volume_ratio"),
            "ma5_state": pa.get("ma5_state"), "ma5_distance_pct": pa.get("ma5_distance_pct"),
            "price_state": pa.get("price_state"), "breakout_5d_pct": pa.get("breakout_5d_pct"),
            "do_not_chase": int(bool(pa.get("do_not_chase"))),
            "legacy_rank": r.get("legacy_rank"), "continuation": r.get("continuation"),
            "base_close": r.get("close"),
            "entry_open": None, "ret_t1": None, "ret_t3": None, "ret_t5": None,
            "ret_t7": None, "net_t3": None, "net_t5": None, "net_t7": None,
            "mfe_t7": None, "mae_t7": None,
            "rule_version": pa.get("rule_version"), "created_at": now,
        })
    if not out:
        return 0
    cols = list(out[0])
    ph = ",".join("?" * len(cols))
    with store.conn(db_path) as c:
        cur = c.executemany(
            f"INSERT OR REPLACE INTO {TABLE} ({','.join(cols)}) VALUES ({ph})",
            [tuple(r[k] for k in cols) for r in out])
        c.commit()
    return cur.rowcount


def backfill(db_path: str = "mls.db") -> int:
    """回填已到期的 T+1/T+3/T+5/T+7 報酬與 MFE/MAE。

    進場價一律用 T+1 開盤 —— 盤後產生的名單在 T0 收盤買不到,
    用收盤價會把隔夜跳空(實測約 +0.94%/日)算成自己的績效。
    """
    ensure(db_path)
    with store.conn(db_path) as c:
        rows = c.execute(
            f"SELECT data_date, code FROM {TABLE} WHERE ret_t7 IS NULL").fetchall()
        n = 0
        for data_date, code in rows:
            bars = c.execute(
                "SELECT data_date, open, high, low, close FROM daily_bar "
                "WHERE code=? AND data_date>? ORDER BY data_date LIMIT 8",
                (code, data_date)).fetchall()
            if len(bars) < 2:
                continue
            entry = bars[0][1]
            if not entry:
                continue
            upd = {"entry_open": entry}
            for h, col in ((1, "ret_t1"), (3, "ret_t3"), (5, "ret_t5"), (7, "ret_t7")):
                if len(bars) >= h:
                    upd[col] = round((bars[h - 1][4] / entry - 1) * 100, 3)
            for h in (3, 5, 7):
                r = upd.get(f"ret_t{h}")
                if r is not None:
                    upd[f"net_t{h}"] = round(r - COST_PCT, 3)
            window = bars[:7]
            if window:
                hi = max(b[2] for b in window if b[2] is not None)
                lo = min(b[3] for b in window if b[3] is not None)
                upd["mfe_t7"] = round((hi / entry - 1) * 100, 3)
                upd["mae_t7"] = round((lo / entry - 1) * 100, 3)
            sets = ",".join(f"{k}=?" for k in upd)
            c.execute(f"UPDATE {TABLE} SET {sets} WHERE data_date=? AND code=?",
                      (*upd.values(), data_date, code))
            n += 1
        c.commit()
    return n
