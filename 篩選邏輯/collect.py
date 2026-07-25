"""
collect.py — 資料採集 producer(唯一會打外部 API 的地方,一天一次)

規範定位:
  這支是「取數通道」,把 mls-v4/app 已驗證的 FinMind 逐檔取數 + scoring +
  absorption,轉成本系統 store 的表。screen_intraday / screen_post 只讀 DB,
  永遠不碰這支。

重用 mls-v4(不重寫取數):
  data_collector.fetch_finmind_{price,inst,margin}  —— 逐檔、免 token、有快取
  analyst._inst_20d / _inst_breakdown / _margin_trend / _price_tech
  scoring.compute_health_score                      —— 資金健康度
  absorption.evaluate                               —— 承接品質

模組名衝突處理:
  mls-v4/app 也有一支 config.py。做法是把 mls-v4/app 插到 sys.path 最前面,
  讓 data_collector→db→config 全解析到 mls-v4;本系統的 UNIVERSE 改用檔案讀取,
  完全不 import 本地 config,兩邊互不打架。store / phase 在 mls-v4 沒有同名檔,
  照常解析到本地。

寫入遵守 store owner 規範:
  inst_flow / margin / daily_bar  -> post_pipeline(死值,INSERT OR IGNORE,不可覆蓋)
  money_health                    -> money_health(可重算,upsert)
  absorption                      -> absorption  (可重算,upsert)

盤中的 quote_snap / aflow 來自 Shioaji 訂閱,不在這支;缺就 NO_DATA,名單照出。
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

# ── 掛上 mls-v4 取數層(必須在 import data_collector 之前) ──────────────
_V4 = Path(__file__).resolve().parents[1] / "mls-v4" / "app"
if not _V4.exists():
    raise RuntimeError(f"找不到 mls-v4 取數層:{_V4}")
sys.path.insert(0, str(_V4))

import data_collector as dc   # noqa: E402  (→ mls-v4)
import analyst                # noqa: E402  (→ mls-v4;內部 import config 也解析到 mls-v4)
import scoring                # noqa: E402  (無 config 依賴)
import absorption as absorp   # noqa: E402  (無 config 依賴)

# ── 本系統模組(mls-v4 無同名檔,照常解析到本地) ──────────────────────
import store                  # noqa: E402
from phase import (            # noqa: E402
    today_tw, get_phase, is_trading_day, last_trading_day, Phase,
)


def _universe() -> list[str]:
    """讀本系統固定 51 檔。不 import 本地 config(避免與 mls-v4 config 撞名)。"""
    ns: dict = {}
    exec((Path(__file__).parent / "config.py").read_text(encoding="utf-8"), ns)
    return list(ns["UNIVERSE"])


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _today_inst(code: str, upto: str) -> dict | None:
    """資料日(含)以前最近一個交易日的法人買賣超(張)。分外資/投信/自營三項。"""
    rows = dc.fetch_finmind_inst(code, days=25)
    if not rows:
        return None
    by_date: dict[str, dict] = {}
    for r in rows:
        if r["date"] > upto:
            continue
        by_date.setdefault(r["date"], {})[r["name"]] = int(r.get("buy", 0)) - int(r.get("sell", 0))
    if not by_date:
        return None
    d = max(by_date)
    b = by_date[d]
    f = (b.get("Foreign_Investor", 0) + b.get("Foreign_Dealer_Self", 0)) // 1000
    t = b.get("Investment_Trust", 0) // 1000
    dl = (b.get("Dealer_self", 0) + b.get("Dealer_Hedging", 0)) // 1000
    return {"date": d, "foreign_net": f, "trust_net": t, "dealer_net": dl,
            "total_net": f + t + dl}


def _scoring_input(code, name, close, prev, chg, inst, brk, mgn, tech) -> scoring.StockInput:
    """複用 mls-v4 analyst 的 StockInput 組法(欄位對齊 scoring.compute_health_score)。"""
    inst_5d_total = brk["foreign_5d"] + brk["invest_5d"] + brk["dealer_5d"]
    quad = ("in_" if chg > 0 else "out_") + ("up" if chg > 0 else "down")
    q_label = {"in_up": "流入↗漲", "in_down": "流入↗跌",
               "out_up": "流出↘漲", "out_down": "流出↘跌"}[quad]
    ratio = 0.05 if inst["net_lots"] > 0 and chg > 0 else (-0.05 if chg < 0 else 0.01)
    return scoring.StockInput(
        code=code, name=name, sector="—", quadrant=q_label,
        day_change_pct=chg, active_buysell_diff=ratio, vol_ratio=1,
        legal_20d_net=inst["net_lots"], foreign_20d=inst["foreign_lots"],
        trust_20d=inst["invest_lots"], legal_5d_net=inst_5d_total,
        foreign_5d=brk["foreign_5d"], trust_5d=brk["invest_5d"],
        legal_consec_days=inst["streak"], margin_5d_chg=mgn["chg_5d"],
        close=close, ma20=tech["ma20"], above_ma20=tech["above_ma20"],
        bias_pct=tech.get("bias", 0),
        foreign_turn_buy=scoring.PASS if brk["foreign_5d"] > 500 else
                         scoring.FAIL if brk["foreign_5d"] < -500 else scoring.NO_DATA,
        margin_down=scoring.PASS if mgn["chg_5d"] < 0 else
                    scoring.FAIL if mgn["chg_5d"] > 0 else scoring.NO_DATA,
        dahu_hold=scoring.NO_DATA,
        price_hold=scoring.FAIL if chg <= -2 else scoring.PASS,
        vs_sector_pct=0, near_limit_up=abs(chg) >= 9, volume_blowout=False,
        no_breakout=close < tech["trigger"], dahu_custody=scoring.NO_DATA,
    ), quad


def collect_one(code: str, d: _dt.date) -> dict:
    """單檔取數 + 計分,回傳各表 rows(未寫入)。缺價 → 回 {} 代表整檔無資料。"""
    dd = d.isoformat()
    prows = dc.fetch_finmind_price(code, days=90)  # old→new
    prows = [r for r in prows if r["date"] <= dd]  # 只用資料日(含)以前,FinMind 會回到今日
    if not prows or len(prows) < 2:
        return {}
    closes = [r["close"] for r in prows]
    vols = [r["Trading_Volume"] for r in prows]
    last = prows[-1]
    close = last["close"]
    prev = closes[-2]
    chg = round((close - prev) / prev * 100, 2) if prev else 0.0

    tech = analyst._price_tech(code, close, prev)
    inst = analyst._inst_20d(code)
    brk = analyst._inst_breakdown(code)
    mgn = analyst._margin_trend(code)
    tinst = _today_inst(code, dd)

    now = _dt.datetime.now().isoformat(timespec="seconds")
    out: dict = {}

    # daily_bar(死值)
    out["daily_bar"] = {
        "code": code, "data_date": dd,
        "open": last.get("open"), "high": last.get("max"), "low": last.get("min"),
        "close": close, "volume": last.get("Trading_Volume"),
        "ma5": round(_mean(closes[-5:]) or close, 2),
        "ma20": tech["ma20"],
        "ma60": round(_mean(closes[-60:]) or close, 2),
        "vol_ma20": int(_mean(vols[-20:]) or 0),
        "source": "finmind", "fetched_at": now,
    }

    # inst_flow(死值)— 缺就不寫這張,screener 標 NO_DATA
    if tinst:
        out["inst_flow"] = {
            "code": code, "data_date": dd,
            "foreign_net": tinst["foreign_net"], "trust_net": tinst["trust_net"],
            "dealer_net": tinst["dealer_net"], "total_net": tinst["total_net"],
            "consecutive_days": inst["streak"],
            "source": "finmind", "fetched_at": now,
        }

    # margin(死值)
    if not mgn.get("data_incomplete"):
        out["margin"] = {
            "code": code, "data_date": dd,
            "margin_balance": mgn["balance"], "margin_change": mgn["chg_5d"],
            "short_balance": None, "short_change": None,
            "source": "finmind", "fetched_at": now,
        }

    # money_health(可重算)
    si, quad = _scoring_input(code, code, close, prev, chg, inst, brk, mgn, tech)
    health = scoring.compute_health_score(si)
    out["money_health"] = {
        "code": code, "data_date": dd,
        "score": health["score"], "quadrant": quad,
        "status": "OK", "reason": health["grade"], "updated_at": now,
    }

    # absorption(可重算)
    snap = {"change_rate": chg, "close": close, "prev_close": prev}
    chip = {"foreign_lots": inst["foreign_lots"],
            "margin_trend": "下降" if mgn["chg_5d"] < 0 else "增加",
            "big_holder_trend": None}
    stars, det = absorp.evaluate(snap, chip)
    out["absorption"] = {
        "code": code, "data_date": dd,
        "score": round(stars / 5 * 100, 1), "grade": det["verdict"],
        "status": "OK", "reason": det["verdict"], "updated_at": now,
    }
    return out


# 表 → (owner, 寫法)。死值用 write_rows(INSERT OR IGNORE,不可覆蓋);計分值用 upsert。
_WRITE = {
    "daily_bar": ("post_pipeline", "insert"),
    "inst_flow": ("post_pipeline", "insert"),
    "margin": ("post_pipeline", "insert"),
    "money_health": ("money_health", "upsert"),
    "absorption": ("absorption", "upsert"),
}


def run(codes: list[str] | None = None, date: _dt.date | None = None,
        db_path: str = "mls.db") -> dict:
    """
    採集全集(預設 51 檔)寫入 DB。
    date 預設為當下時段對應的資料日(POST=今日,PRE/INTRADAY=昨日死值)。
    """
    store.init_db(db_path)
    codes = codes or _universe()

    # 交易日守門:排程若在週末/國定假日觸發,直接略過,不打任何 API。
    # 要補跑特定歷史交易日,帶明確 date 即可繞過。
    if date is None:
        if not is_trading_day(today_tw()):
            print(f"今天 {today_tw()} 非交易日(休市),略過採集。"
                  f"要補跑請帶 --date YYYY-MM-DD。")
            return {"data_date": None, "skipped": "non_trading_day",
                    "universe": len(codes), "written": {t: 0 for t in _WRITE},
                    "no_data": [], "watchlist_post": 0}
        # 交易日:盤後收今日,盤前/盤中收上一交易日(今日尚未收盤)
        d = today_tw() if get_phase() is Phase.POST else last_trading_day(today_tw() - _dt.timedelta(days=1))
    else:
        d = date

    written = {t: 0 for t in _WRITE}
    empty: list[str] = []

    for code in codes:
        try:
            rows = collect_one(code, d)
        except Exception as e:
            print(f"  ✗ {code}: {type(e).__name__}: {e}")
            empty.append(code)
            continue
        if not rows:
            empty.append(code)
            continue
        for table, row in rows.items():
            owner, how = _WRITE[table]
            try:
                if how == "insert":
                    store.write_rows(table, owner, [row], db_path)
                else:
                    store.upsert_intraday(table, owner, [row], db_path)
                written[table] += 1
            except store.ImmutableDataError:
                pass  # 已收盤死值抓過一次不再覆蓋,正常
            except Exception as e:
                print(f"  ✗ {code}/{table}: {type(e).__name__}: {e}")

    # 盤後死值存指紋(之後有插件動到就報錯)
    try:
        store.snapshot_post(d, db_path)
    except Exception:
        pass

    # 灌完來源表後,順手把「當日盤後名單」落地到 watchlist_post。
    # 這是關鍵一步:盤前(PRE)不重算,只讀昨日這份名單;沒落地,隔天盤前就空。
    import screen_post
    watchlist_n = 0
    try:
        wl = screen_post.build(codes, db_path, d)
        watchlist_n = len(wl["items"])
    except Exception as e:
        print(f"  ✗ watchlist_post 落地失敗: {type(e).__name__}: {e}")

    return {"data_date": d.isoformat(), "universe": len(codes),
            "written": written, "no_data": empty, "watchlist_post": watchlist_n}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="MLS 資料採集(重用 mls-v4 取數)")
    ap.add_argument("--date", help="YYYY-MM-DD,預設當下時段資料日")
    ap.add_argument("--codes", help="逗號分隔,測試用;預設全 51 檔")
    ap.add_argument("--db", default="mls.db")
    a = ap.parse_args()
    dd = _dt.date.fromisoformat(a.date) if a.date else None
    cs = a.codes.split(",") if a.codes else None
    print("=" * 60)
    print(f"採集開始 date={dd or '(當下交易日)'} codes={len(cs) if cs else '51(全集)'}")
    print("=" * 60)
    r = run(cs, dd, a.db)
    if r.get("skipped"):
        raise SystemExit(0)
    print("-" * 60)
    print(f"資料日 {r['data_date']}  全集 {r['universe']} 檔")
    for t, n in r["written"].items():
        print(f"  {t:14} 寫入 {n}")
    print(f"  {'watchlist_post':14} 落地 {r['watchlist_post']}(明日盤前讀這份)")
    print(f"  無資料 {len(r['no_data'])} 檔:{r['no_data'][:10]}{'...' if len(r['no_data']) > 10 else ''}")
