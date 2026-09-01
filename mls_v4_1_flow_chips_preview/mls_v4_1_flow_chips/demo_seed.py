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
            ("2455","全新", 47_300_000, 4.2, 110,112,100,106,1.1, 1800,0,1),
            ("2344","華邦電", 38_500_000, 1.6, 104,106,100,102,1.0, 1200,0,1),
            ("3037","欣興", 31_200_000, 2.1, 108,110,100,105,1.2,-2500,0,1),
            ("3006","晶豪科", 25_800_000, 3.0, 106,108,100,103,1.0, 900,0,1),
            ("2337","旺宏", 18_900_000, .8, 102,104,100,101,1.1,-600,0,1),
            ("3374","精材",-36_000_000,-1.0,109,110,100,106,1.8, 8000,1,0),
            ("3363","上詮",-28_500_000,-.6,109.2,110,100,106,.6, 2100,1,0),
            ("6213","聯茂",-22_400_000,-2.4,104,110,100,106,1.1,-830,1,0),
            ("2481","強茂",-18_700_000,-1.8,103.5,110,100,106,1.8,14920,1,0),
            ("5425","台半",-12_600_000,-1.2,101,110,100,104,1.1,-49,1,0),
        ]
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
            for d in ("2026-08-25","2026-08-26","2026-08-27","2026-08-28"):
                c.execute("INSERT INTO chip_daily VALUES(?,?,?,?,?,?,?,?)",
                          (d,symbol,per_day,per_day,25000,.1,d,d+"T16:00:00+08:00"))
            c.execute("INSERT INTO trigger_context VALUES(?,?,?,?,?,?,?,?,?)",
                      ("2026-08-28",symbol,105,103,tf,tp,"2026-08-28","2026-08-28T15:10:00+08:00","DEMO"))

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
