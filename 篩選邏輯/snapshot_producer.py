"""snapshot_producer.py — b_snapshot 盤中時序 producer

盤中每 5 分鐘,把 feedbridge 已寫進 quote_snap/aflow 的即時值落成 b_snapshot 時序。
漏斗(funnel) L1/L2/L2.5 全靠這份時序;沒有它每天真實跑都是 0 檔。

鐵律:不登入 Shioaji。同金鑰多重登入會互踢行情 session(見 memory
      shioaji-session-war-orphans)。行情來源唯一就是 feedbridge 已落地的
      quote_snap(價量內外盤) + aflow(主動買賣淨額)。

非盤中時 b_snapshot.take() 自身 no-op(phase 閘),所以 timer 打太早/太晚都安全。

2026-08-26:quote_snap 與 aflow 是兩張各自獨立更新的表,合併時原本沒比對兩者
updated_at,可能拼出「新報價 + 舊 aflow」的一格(見 memory
b-snapshot-2026-08-05-incident——08-06/07/10 抓到 net_active 卡住數十分鐘不動、
quote 卻正常在動的實例)。這裡只把兩邊 updated_at 與時間差算出來一起落地,純觀察,
不做任何 stale 判斷、不因此丟資料或回填 None——要等這份 freshness_gap_sec 累積
夠多天,才回頭研究多少秒算 stale。
"""
from __future__ import annotations

import sqlite3
import datetime as _dt

import b_snapshot
from phase import today_tw

DB = "mls.db"


def _seconds_between(a: str | None, b: str | None) -> float | None:
    """兩個 ISO timestamp 相差幾秒(a-b)。任一邊缺值就回傳 None,不用 0 頂替。"""
    if not a or not b:
        return None
    try:
        return (_dt.datetime.fromisoformat(a) - _dt.datetime.fromisoformat(b)).total_seconds()
    except ValueError:
        return None


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
        ar = a.get(code) or {}
        buf[code] = {
            "price": qr.get("price"),
            "change_rate": qr.get("change_rate"),
            "volume": qr.get("volume"),
            "bid_vol": qr.get("bid_vol"),
            "ask_vol": qr.get("ask_vol"),
            "net_active": ar.get("net_active"),
            "quote_updated_at": qr.get("updated_at"),
            "aflow_updated_at": ar.get("updated_at"),
            "freshness_gap_sec": _seconds_between(qr.get("updated_at"), ar.get("updated_at")),
            "aflow_method": ar.get("method"),
        }
    return buf


def main() -> None:
    buf = build_buffer()
    res = b_snapshot.take(buf)
    print(f"[snapshot] buffer={len(buf)} 檔 → {res}")


if __name__ == "__main__":
    main()
