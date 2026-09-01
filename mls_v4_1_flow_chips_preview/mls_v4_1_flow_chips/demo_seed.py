"""Create an isolated demonstration DB. Never used automatically by the app."""
from __future__ import annotations

import sys
from pathlib import Path

from .repository import apply_schema, open_db

PKG = Path(__file__).resolve().parent


def seed(path: str | Path) -> None:
    path = Path(path)
    if path.exists():
        path.unlink()
    with open_db(path) as c:
        apply_schema(c, PKG / "schema.sql")
        c.execute("INSERT INTO flow_threshold_config VALUES(?,?,?,?)", ("default", 10_000_000, 2, "DEMO ONLY"))
        c.execute("INSERT INTO market_regime_daily VALUES(?,?,?,?,?)", ("2026-09-01", "RISK_ON", -0.2, 0.50, "2026-09-01T09:00:00+08:00"))

        stocks = [
            # symbol, name, flow, chg, close, high, low, vwap, volume/ma5, foreign4d, trigger_failed, trigger_passed
            ("2455","全新", 47_300_000, 4.2, 110,112,100,106,1.1, 1800,0,1),
            ("2344","華邦電", 38_500_000, 1.6, 104,106,100,102,1.0, 1200,0,1),
            ("3037","欣興", 31_200_000, 2.1, 108,110,100,105,1.2,-2500,0,1),
            ("3006","晶豪科", 25_800_000, 3.0, 106,108,100,103,1.0, 900,0,1),
            ("2337","旺宏", 18_900_000, .8, 102,104,100,101,1.1,-600,0,1),
            ("3363","上詮",-28_500_000,-.6,109.2,110,100,106,.6, 2100,1,0),
            ("6213","聯茂",-22_400_000,-2.4,104,110,100,106,1.1,-830,1,0),
            ("2481","強茂",-18_700_000,-1.8,103.5,110,100,106,1.8,14920,1,0),
            ("5425","台半",-12_600_000,-1.2,101,110,100,104,1.1,-49,1,0),
        ]
        chip_dates = [f"2026-08-{d:02d}" for d in range(10, 29)] + ["2026-08-31"]
        for symbol,name,flow,chg,close,high,low,vwap,vr,f4,tf,tp in stocks:
            for i,ts in enumerate(("10:55:00","11:00:00")):
                c.execute("""INSERT INTO intraday_snapshot(
                    trade_date,symbol,stock_name,ts,open,high,low,close,prev_close,volume,ma5_volume,
                    vwap,a_flow,net_active,bid_ask_ratio,net_flow_amount,turnover_ratio,price_change_pct,
                    price_data_date,flow_data_time,as_of)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("2026-09-01",symbol,name,ts,100,high,low,close,100,vr*1000,1000,vwap,
                     100+i,10,1.2,flow*(.92 if i==0 else 1),.08,chg,"2026-09-01",ts,
                     f"2026-09-01T{ts}+08:00"))
            per_day = f4 / 4
            for d in chip_dates:
                c.execute("INSERT INTO chip_daily VALUES(?,?,?,?,?,?,?,?)",
                          (d,symbol,per_day,per_day,25000,.1,d,d+"T16:00:00+08:00"))
            c.execute("INSERT INTO trigger_context VALUES(?,?,?,?,?,?,?,?,?)",
                      ("2026-08-31",symbol,105,103,tf,tp,"2026-08-31","2026-08-31T15:10:00+08:00","DEMO"))

        # Research-only Day-1 reversal examples. Observed values are kept where known;
        # supporting VWAP/volume fields are layout-only demo inputs until the VPS DB is connected.
        reversal_examples = [
            ("6182", "合晶", [("10:59:00", 112.5, 8872, 6.63, 111.8)],
             "A+ RESULT: 10:59 A-flow +8,872 at about +6.6%; 11:45 price 116, +9.95% limit-up. R4 second A-flow sample not observed, so do not fabricate Persistence."),
            ("8150", "南茂", [("10:59:00", 93.9, 13107, 5.2, 92.8),
                               ("11:41:00", 94.6, 15779, 6.0, 93.1)],
             "A sample: A-flow +13,107 -> +15,779 while price 93.9 -> 94.6."),
            ("3532", "台勝科", [("10:59:00", 400.5, 604, 5.5, 398.0),
                                  ("11:41:00", 405.0, 1061, 6.7, 401.0)],
             "B+ sample: A-flow +604 -> +1,061 while price 400.5 -> 405."),
            ("3374", "精材", [("11:41:00", 421.0, -1322, 5.0, 419.0)],
             "NEGATIVE CONTROL: A-flow -1,322; not part of the Day-1 flow-reversal type."),
        ]
        for symbol, name, snaps, note in reversal_examples:
            for d in chip_dates:
                c.execute("INSERT OR REPLACE INTO chip_daily VALUES(?,?,?,?,?,?,?,?)",
                          (d, symbol, -500, -1000, 10000, -.1, d, d+"T16:00:00+08:00"))
            for ts, close, aflow, chg, vwap in snaps:
                prev_close = close / (1 + chg/100)
                c.execute("""INSERT INTO intraday_snapshot(
                    trade_date,symbol,stock_name,ts,open,high,low,close,prev_close,volume,ma5_volume,
                    vwap,a_flow,net_active,bid_ask_ratio,net_flow_amount,turnover_ratio,price_change_pct,
                    price_data_date,flow_data_time,as_of)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("2026-09-01",symbol,name,ts,prev_close,close+1,close-2,close,prev_close,100000,200000,
                     vwap,aflow,100,1.2,aflow*1000,.08,chg,"2026-09-01",ts,
                     f"2026-09-01T{ts}+08:00"))
            c.execute("INSERT OR REPLACE INTO trigger_context VALUES(?,?,?,?,?,?,?,?,?)",
                      ("2026-08-31",symbol,None,None,None,None,"2026-08-31",
                       "2026-08-31T15:10:00+08:00",note))

        # UMC 2303 is deliberately a control, not a reversal success. We only have one
        # verified prior-day chip observation, so R1 5D/20D must remain NO_DATA/false.
        c.execute("INSERT OR REPLACE INTO chip_daily VALUES(?,?,?,?,?,?,?,?)",
                  ("2026-08-31","2303",-12716,-7502,None,None,"2026-08-31","2026-08-31T16:00:00+08:00"))
        c.execute("""INSERT INTO intraday_snapshot(
            trade_date,symbol,stock_name,ts,open,high,low,close,prev_close,volume,ma5_volume,
            vwap,a_flow,net_active,bid_ask_ratio,net_flow_amount,turnover_ratio,price_change_pct,
            price_data_date,flow_data_time,as_of)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("2026-09-01","2303","聯電","11:00:00",129,129,128,129,129,165000,100000,
             129,17963,100,1.2,None,.08,0.0,"2026-09-01","11:00:00",
             "2026-09-01T11:00:00+08:00"))
        c.execute("INSERT OR REPLACE INTO trigger_context VALUES(?,?,?,?,?,?,?,?,?)",
                  ("2026-08-31","2303",129,129,0,1,"2026-08-31","2026-08-31T15:10:00+08:00",
                   "CONTROL: official 8/31 foreign -12,716 lots; investment trust +6,340; total institutions -7,502. Intraday RVOL 1.65x; A-flow +17,963; price trigger 129 hit; Acceptance NO/FAILED; final action NO_ENTRY."))

        rates = {
            "INFLOW_STRONG__CHIP_GOOD": .70,
            "INFLOW_STRONG__CHIP_WEAK": .44,
            "OUTFLOW_STRONG__CHIP_GOOD__RESCUE_HIGH": .68,
            "OUTFLOW_STRONG__CHIP_WEAK__TRUE_FAIL": .25,
            "OUTFLOW_STRONG__CHIP_GOOD__RESCUE": .60,
            "OUTFLOW_STRONG__CHIP_GOOD__FLOW_PRICE_DIVERGENCE": .30,
        }
        for scenario, rate in rates.items():
            n = 30
            wins = round(n*rate)
            for i in range(n):
                win = 1 if i < wins else 0
                c.execute("""INSERT INTO decision_history(
                    trade_date,symbol,scenario,state,market_regime,success,next_day_up,plus3,plus5,mfe,mae,baseline_up_rate,was_false_kill)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("2026-08-01","DEMO",scenario,"DEMO","RISK_ON",win,win,1 if i<wins//2 else 0,
                     1 if i<wins//4 else 0,3.2,-1.4,.50,0))
        c.commit()
    print(path)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else str(PKG.parent / "demo.db")
    seed(target)
