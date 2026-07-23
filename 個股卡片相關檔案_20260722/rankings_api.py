"""
MLS 插件 — rankings_api.py
排行頁資料模組:供 /api/eod_rank 使用,盤後榜單全部取自
v1.4 EOD 管線落地的 training_samples / sector_daily / signals。
不動主系統任何邏輯。
"""

import json
import config as C
import db


def _latest_date():
    with db._lock, db._conn() as c:
        r = c.execute("SELECT MAX(trade_date) d FROM training_samples").fetchone()
        return r["d"]


def eod_rankings():
    """回傳盤後榜單 JSON:五榜 + 族群卡 + 訊號統計。無資料時 note 提示。"""
    d = _latest_date()
    if not d:
        return {"date": None, "note": "尚無盤後資料(EOD 管線於收盤後 15:05 產出)"}

    rows = []
    with db._lock, db._conn() as c:
        for r in c.execute("""SELECT stock_id, features, close_price, label
                              FROM training_samples WHERE trade_date=?""", (d,)):
            try:
                f = json.loads(r["features"])
            except Exception:
                continue
            rows.append({
                "code": r["stock_id"],
                "name": C.NAME_MAP.get(r["stock_id"], r["stock_id"]),
                "sector": f.get("sector", "—"),
                "close": r["close_price"],
                "chg": f.get("chg") or 0,
                "vr": f.get("vr") or 0,
                "tnvr": f.get("tnvr") or 0,
                "at_high": f.get("at_high") or 0,
                "rs": f.get("rs_sector") or 0,
                "inst_net": f.get("inst_net"),
                "heat": round(abs(f.get("chg") or 0) * 3
                              + (f.get("vr") or 0) * 4
                              + (f.get("at_high") or 0) * 5, 1),
                "label": r["label"],           # 隔日結果(已回填才有)
            })

        sec_rows = [dict(x) for x in c.execute(
            """SELECT sector, pct, amount_share, flow_dir, quadrant
               FROM sector_daily WHERE trade_date=? ORDER BY pct DESC""", (d,))]

    # fallback:當日 sector_daily 缺列(主系統盤後未跑/中斷)→ 由樣本聚合
    if not sec_rows:
        from statistics import median
        g = {}
        for x in rows:
            g.setdefault(x["sector"], []).append(x["chg"])
        sec_rows = sorted(
            [{"sector": k, "pct": round(median(v), 2), "amount_share": None,
              "flow_dir": 1 if median(v) > 0 else -1, "quadrant": ""}
             for k, v in g.items() if k != "—"],
            key=lambda z: -z["pct"])

        sig = c.execute("""SELECT COUNT(*) total,
            SUM(CASE WHEN action='buy' THEN 1 ELSE 0 END) buys,
            SUM(CASE WHEN action='sell' THEN 1 ELSE 0 END) risks
            FROM signals WHERE trade_date=?""", (d,)).fetchone()

    def top(key, rev=True, n=10):
        return sorted(rows, key=lambda x: x[key], reverse=rev)[:n]

    return {
        "date": d,
        "hot": top("heat"),
        "gainers": top("chg"),
        "losers": sorted(rows, key=lambda x: x["chg"])[:10],
        "volume": top("tnvr"),
        "newhigh": [x for x in sorted(rows, key=lambda x: -x["chg"]) if x["at_high"]][:10],
        "sectors": sec_rows,
        "signals": dict(sig) if sig else {},
        "n": len(rows),
    }
