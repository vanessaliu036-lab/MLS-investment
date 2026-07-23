"""
MLS 模組 — health_timeseries.py(v3.1 新增)
補上「時間序列」這隻從沒裝上的腳
====================================================================
使用者核心診斷:個股健康指數該是時間序列——「今天比昨天健康嗎、
資金連續幾天」。但 money_health.stock_health() 只吃當下一筆快照,
從不讀昨天,所以畫面永遠顯示「趨勢新增、資金連續1日」。

本模組每日盤後把每檔健康快照落地 mls.db(health_daily 表),
並提供「讀昨日 → today vs prev」比對,產出真正的:
  flow_streak     資金流入連續天數(in_up/in_down 連續)
  health_trend    改善 / 持平 / 惡化(對比昨日象限位階)
  quad_history    近5日象限序列(給前端畫「資金軌跡」)

純加表,不動主 schema。與 decision_v22 的 dec_health 並存不衝突
(dec_health 是決策層落地;這裡是健康指數層落地,兩者可日後合併)。

離線可完整驗證:純 SQLite 邏輯,不依賴網路。見 __main__ 三日測試。
"""

from datetime import datetime, timedelta, timezone

import db

TW_TZ = timezone(timedelta(hours=8))

QUAD_RANK = {"out_down": 0, "in_down": 1, "out_up": 2, "in_up": 3}


def _init():
    with db._lock, db._conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS health_daily(
          trade_date TEXT, code TEXT,
          quadrant TEXT, health_score INTEGER,
          aflow_ratio REAL, chg REAL,
          price_s INTEGER, flow_s INTEGER, chip_s INTEGER, sector_s INTEGER,
          chip_quality TEXT,
          flow_streak INTEGER, health_trend TEXT,
          PRIMARY KEY(trade_date, code)
        );
        CREATE INDEX IF NOT EXISTS idx_health_daily_code
          ON health_daily(code, trade_date);
        """)


def _today():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


def _prev_row(code, before_date):
    with db._lock, db._conn() as c:
        r = c.execute("""SELECT * FROM health_daily
                         WHERE code=? AND trade_date<?
                         ORDER BY trade_date DESC LIMIT 1""",
                      (code, before_date)).fetchone()
        return dict(r) if r else None


def enrich_and_persist(snaps, trade_date=None):
    """
    盤後呼叫。snaps 需已經過 money_health.annotate()(每檔有 _health)。
    對每檔:讀昨日 → 算 flow_streak / health_trend → 落地 health_daily →
    把結果寫回 s["_health"]["flow_streak"] / ["health_trend"](前端直接用)。
    """
    _init()
    tdate = trade_date or _today()
    for s in snaps:
        h = s.get("_health")
        if not h:
            continue
        code = s["code"]
        quad = h["quadrant"]
        prev = _prev_row(code, tdate)
        if prev is None:
            streak = 1 if quad.startswith("in") else 0
            trend = "首日"
        else:
            # 資金連續:今昨都 in_* 才累加,否則重算
            if quad.startswith("in") and str(prev["quadrant"]).startswith("in"):
                streak = (prev["flow_streak"] or 0) + 1
            else:
                streak = 1 if quad.startswith("in") else 0
            d = QUAD_RANK[quad] - QUAD_RANK.get(prev["quadrant"], 1)
            trend = "改善" if d > 0 else ("惡化" if d < 0 else "持平")

        ms = h.get("module_scores", {})
        with db._lock, db._conn() as c:
            c.execute("""INSERT OR REPLACE INTO health_daily VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (tdate, code, quad, h.get("health_score"),
               h.get("aflow_ratio"), s.get("change_rate"),
               ms.get("price"), ms.get("flow"), ms.get("chip"), ms.get("sector"),
               h.get("chip_quality"), streak, trend))
        # 寫回前端可直接讀
        h["flow_streak"] = streak
        h["health_trend"] = trend
    return snaps


def quad_history(code, days=5):
    """近 N 日象限序列(舊→新),給前端畫資金軌跡。"""
    _init()
    with db._lock, db._conn() as c:
        rows = c.execute("""SELECT trade_date, quadrant, health_score
                            FROM health_daily WHERE code=?
                            ORDER BY trade_date DESC LIMIT ?""",
                         (code, days)).fetchall()
    return [dict(r) for r in reversed(rows)]


if __name__ == "__main__":
    # 離線完整驗證:模擬三天,確認 streak/trend 真的會動
    import os
    db_path = getattr(db, "DB_PATH", "mls.db")
    if os.path.exists("mls.db"):
        os.remove("mls.db")

    def fake_snaps(quad_map, chg=1.0):
        out = []
        for code, quad in quad_map.items():
            out.append({"code": code, "change_rate": chg,
                        "_health": {"quadrant": quad, "health_score": 70,
                                    "aflow_ratio": 0.1,
                                    "module_scores": {"price": 60, "flow": 55,
                                                      "chip": None, "sector": 65},
                                    "chip_quality": "finmind_basic"}})
        return out

    print("=== D1: A=in_up, B=out_down ===")
    s1 = enrich_and_persist(fake_snaps({"A": "in_up", "B": "out_down"}),
                            "2026-07-09")
    for s in s1:
        print(f"  {s['code']}: streak={s['_health']['flow_streak']} "
              f"trend={s['_health']['health_trend']}")

    print("=== D2: A=in_up(連續應=2), B=in_down(轉入應=1,改善) ===")
    s2 = enrich_and_persist(fake_snaps({"A": "in_up", "B": "in_down"}),
                            "2026-07-10")
    for s in s2:
        print(f"  {s['code']}: streak={s['_health']['flow_streak']} "
              f"trend={s['_health']['health_trend']}")
    a = next(s for s in s2 if s["code"] == "A")
    b = next(s for s in s2 if s["code"] == "B")
    assert a["_health"]["flow_streak"] == 2, "A 資金應連續2日"
    assert b["_health"]["flow_streak"] == 1 and b["_health"]["health_trend"] == "改善"

    print("=== D3: A=out_up(斷,惡化), 象限歷史 ===")
    s3 = enrich_and_persist(fake_snaps({"A": "out_up"}, chg=0.5), "2026-07-11")
    a = s3[0]
    print(f"  A: streak={a['_health']['flow_streak']} trend={a['_health']['health_trend']}")
    assert a["_health"]["flow_streak"] == 0 and a["_health"]["health_trend"] == "惡化"
    print("  A 象限歷史:", [r["quadrant"] for r in quad_history("A")])
    print("\n✅ 時間序列驗證通過:streak 會累加會歸零、trend 會改善會惡化")
    os.remove("mls.db")
