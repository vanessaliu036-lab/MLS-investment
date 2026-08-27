"""line_b_watch_ledger.py — Line B C1+C2 觀察 ledger(append-only,唯讀展示用)

⚠ 這是 Line B 研究產物的紀錄層,不是 production gate。不讀 Line A 任何表、
   不寫 candidate_pool/watchlist_post,不影響任何既有 tier/track/score。

凍結定義(2026-08-26 One-Shot Acceptance 封板,不得因為新資料調整):
    C1 STRUCTURE_INTACT               : close >= MA20
    C2 SELLING_PRESSURE_WEAKENING     : price_5d > 0 AND close_position >= 0.7
                                         AND NOT(inst_5d <= -3000)
    flow_class(事件制,非時間點)       : OPEN_POSITIVE / FLOW_FLIP / NO_FLIP
                                         (沿用 persistent_flow_flip.py 定義)
    WATCH MODE activation             : confirmed_reversal(close>prior_high
                                         AND close>=ma20 AND net_active>0)

時序鐵律:今天盤中才冒出的股票(不在昨晚 C1+C2 名單)可以標成當天的
INTRADAY_DISCOVERY,收盤後重新算 C1/C2 可以進明天的名單 —— 但不能回頭
把它寫成「昨晚就被 C1+C2 選中」。source 欄位就是防這件事的稽核鎖。

驗證樣本標示(2026-08-26 封板時):
    C1+C2 歷史啟動率 ≈ 64.1%(11天,n=561,day-equal)
    A-flow CONFIRMED(OPEN_POSITIVE/FLOW_FLIP) 歷史啟動率 ≈ 89.9%
    A-flow NO_FLIP 歷史啟動率 ≈ 2.8%
    ⚠ 這是「這批乾淨歷史樣本」的回顧結果,不是未來保證。本表存在的目的就是
    每天累積 forward 樣本,重新算這三個數字有沒有維持。
"""
from __future__ import annotations

import datetime as _dt
import hashlib as _hashlib

import store

TABLE = "line_b_watch_ledger"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    data_date TEXT NOT NULL,           -- T0(這一列描述的交易日)
    code TEXT NOT NULL,
    -- ── source:稽核鎖,防止時序造假 ──
    source TEXT NOT NULL,              -- C1C2_PASS(昨晚T-1收盤通過) /
                                        -- INTRADAY_DISCOVERY(今天盤中才冒出,
                                        --   昨晚未通過 C1+C2)
    -- ── T-1 收盤凍結狀態(source=C1C2_PASS 才有意義;INTRADAY_DISCOVERY 為 NULL)──
    c1_structure_intact INTEGER,
    c2_selling_weak_price_resp INTEGER,
    t1_close REAL, t1_ma20 REAL, t1_prior_high REAL,
    t1_inst_5d REAL, t1_price_5d REAL, t1_close_position REAL,
    -- ── T 日盤中(當天收盤後定案,append-only)──
    flow_class TEXT,                   -- OPEN_POSITIVE / FLOW_FLIP / NO_FLIP / NULL(無盤中資料)
    flow_confirm_magnitude REAL,       -- 確認當下的 net_active 幅度(排序用,非時間點)
    watch_mode_activated INTEGER,      -- 是否觸發 confirmed_reversal
    activation_slot TEXT,              -- 觸發格(供追溯,不作排序依據)
    t_high REAL, t_low REAL, t_close REAL,
    -- ── 收盤後重算(供隔日觀察名單用,同樣時序鎖:只能寫「今天收盤後算出」)──
    eod_c1 INTEGER, eod_c2 INTEGER,
    enters_next_day_watchlist INTEGER, -- eod_c1 AND eod_c2
    -- ── 稽核 ──
    definition_version TEXT NOT NULL,  -- 對應凍結定義版本,門檻改變一定要 bump
    row_hash TEXT,
    created_at TEXT,
    PRIMARY KEY (data_date, code)
);
"""

DEFINITION_VERSION = "line_b_c1c2_v1_2026-08-26"

_HASH_KEYS = (
    "data_date", "code", "source",
    "c1_structure_intact", "c2_selling_weak_price_resp",
    "flow_class", "watch_mode_activated", "activation_slot",
    "eod_c1", "eod_c2", "definition_version",
)


class RetroactiveWriteRefused(RuntimeError):
    """試圖覆寫較舊交易日的列——ledger 一旦寫入即凍結。"""


class LedgerMutationRefused(RuntimeError):
    """同日重跑但語意輸入變了(source/C1/C2/flow_class/definition_version 任一不同)。

    不得靜默覆寫,那等於事後把時序竄改成「早就知道」。
    """


def ensure(db_path: str = "mls.db") -> None:
    with store.conn(db_path) as c:
        c.executescript(DDL)


def _row_hash(row: dict) -> str:
    payload = "\n".join(f"{k}={row.get(k)!r}" for k in _HASH_KEYS)
    return _hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_rows(data_date: _dt.date, rows: list[dict], db_path: str = "mls.db") -> dict:
    """Append-only 寫入。同日同碼重跑:語意相同 → no-op;語意不同 → 拒絕。
    較舊日期一律拒絕(ledger 只能往前寫,不能回頭改)。
    """
    ensure(db_path)
    d = data_date.isoformat() if hasattr(data_date, "isoformat") else str(data_date)
    now = _dt.datetime.now().isoformat(timespec="seconds")
    written, skipped_noop = 0, 0

    with store.conn(db_path) as c:
        existing_max = c.execute(f"SELECT MAX(data_date) FROM {TABLE}").fetchone()[0]
        if existing_max and d < existing_max:
            raise RetroactiveWriteRefused(
                f"ledger 最新日期已到 {existing_max},不可回頭寫 {d}")

        for r in rows:
            row = {**r, "data_date": d, "definition_version": DEFINITION_VERSION}
            h = _row_hash(row)
            prior = c.execute(
                f"SELECT row_hash FROM {TABLE} WHERE data_date=? AND code=?",
                (d, row["code"]),
            ).fetchone()
            if prior is not None:
                if prior[0] == h:
                    skipped_noop += 1
                    continue
                raise LedgerMutationRefused(
                    f"{d} {row['code']} 語意已變(source/C1/C2/flow_class/definition_version"
                    f" 任一不同),拒絕靜默覆寫;若是真的要修正,需要人工確認後刪列重寫。")
            row["row_hash"] = h
            row["created_at"] = now
            cols = list(row.keys())
            ph = ",".join("?" * len(cols))
            c.execute(f"INSERT INTO {TABLE} ({','.join(cols)}) VALUES ({ph})",
                     [row[k] for k in cols])
            written += 1
        c.commit()
    return {"written": written, "noop": skipped_noop}


def historical_activation_rates(db_path: str = "mls.db") -> dict:
    """給前端顯示用的三個既有驗證數字。只讀,不重算——這些是封板時的離線結果,
    不是即時算出來的(即時重算要在累積更多 forward 天數後才有意義,見 header)。
    """
    return {
        "c1_c2_pass_rate_label": "64.1%",
        "c1_c2_pass_sample_note": "11 clean days, n=561, day-equal, 2026-08-26 One-Shot Acceptance",
        "flow_confirmed_rate_label": "89.9%",
        "flow_confirmed_sample_note": "same sample, OPEN_POSITIVE/FLOW_FLIP subset",
        "flow_no_flip_rate_label": "2.8%",
        "caveat": "Retrospective on the available clean-day sample. Not a forward guarantee. "
                  "Track this ledger's own forward numbers as they accumulate.",
    }
