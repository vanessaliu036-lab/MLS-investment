"""snapshot_producer.py — b_snapshot 盤中時序 producer

盤中每 5 分鐘,把 feedbridge 已寫進 quote_snap/aflow 的即時值落成 b_snapshot 時序。
漏斗(funnel) L1/L2/L2.5 全靠這份時序;沒有它每天真實跑都是 0 檔。

鐵律:不登入 Shioaji。同金鑰多重登入會互踢行情 session(見 memory
      shioaji-session-war-orphans)。行情來源唯一就是 feedbridge 已落地的
      quote_snap(價量內外盤) + aflow(主動買賣淨額)。

非盤中時 b_snapshot.take() 自身 no-op(phase 閘),所以 timer 打太早/太晚都安全。
"""
from __future__ import annotations

import sqlite3

import b_snapshot
from phase import today_tw

DB = "mls.db"


def build_buffer(db_path: str = DB) -> dict[str, dict]:
    """讀今日 quote_snap + aflow,組成 b_snapshot.take() 要的 buffer。無價的檔跳過。"""
    d = today_tw().isoformat()
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        q = {r["code"]: dict(r) for r in
             c.execute("SELECT * FROM quote_snap WHERE data_date=?", (d,))}
        a = {r["code"]: dict(r) for r in
             c.execute("SELECT * FROM aflow WHERE data_date=?", (d,))}
    finally:
        c.close()

    buf: dict[str, dict] = {}
    for code, qr in q.items():
        if qr.get("price") is None:
            continue
        buf[code] = {
            "price": qr.get("price"),
            "change_rate": qr.get("change_rate"),
            "volume": qr.get("volume"),
            "bid_vol": qr.get("bid_vol"),
            "ask_vol": qr.get("ask_vol"),
            "net_active": (a.get(code) or {}).get("net_active"),
        }
    return buf


def main() -> None:
    buf = build_buffer()
    res = b_snapshot.take(buf)
    print(f"[snapshot] buffer={len(buf)} 檔 → {res}")


if __name__ == "__main__":
    main()
