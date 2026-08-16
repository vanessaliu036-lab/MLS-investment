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
import official_price        # noqa: E402
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


def _signed_streak(series: list[int]) -> int:
    """帶號連續天數。series：舊→新的每日淨額。以最後一日方向為準,往回數同號連續天。
    正=連買天數、負=連賣天數、0=最後一日持平或無資料。funnel L3 據此:連買加分、
    連賣≤-3 硬砍(見 funnel.layer3)。中間出現 0(持平)即中斷連續。"""
    if not series:
        return 0
    last = series[-1]
    if last == 0:
        return 0
    pos = last > 0
    n = 0
    for v in reversed(series):
        if v != 0 and (v > 0) == pos:
            n += 1
        else:
            break
    return n if pos else -n


def _today_inst(code: str, upto: str) -> dict | None:
    """資料日(含)以前最近一個交易日的法人買賣超(張)。分外資/投信/自營三項。
    另計三法人各自帶號連續天數(foreign_days/trust_days/dealer_days),供 L3 籌碼層。"""
    official = {}
    try:
        ymd = upto.replace("-", "")
        official.update(dc.fetch_twse_inst_today(ymd) or {})
        official.update(dc.fetch_tpex_inst_today(ymd) or {})
    except Exception:
        official = {}
    rec = official.get(str(code))
    if rec:
        # Official same-day data has the correct net values but no streak.
        # Build each streak from the persisted, date-ordered history plus
        # today's official row; never default these fields to zero.
        prior = []
        try:
            import store
            prior = store.read_recent("inst_flow", str(code), _dt.date.fromisoformat(upto), 25)
            prior = [r for r in prior if r.get("data_date") < upto]
            prior.sort(key=lambda r: r["data_date"])
            f_series = [r.get("foreign_net") or 0 for r in prior] + [rec.get("foreign_lots") or 0]
            t_series = [r.get("trust_net") or 0 for r in prior] + [rec.get("invest_lots") or 0]
            d_series = [r.get("dealer_net") or 0 for r in prior] + [rec.get("dealer_lots") or 0]
            f_days, t_days, d_days = (_signed_streak(f_series),
                                     _signed_streak(t_series),
                                     _signed_streak(d_series))
        except Exception:
            f_days = t_days = d_days = 0
        total_series = [r.get("total_net") or 0 for r in prior] + [rec.get("total_lots") or 0]
        return {"date": upto, "foreign_net": rec.get("foreign_lots"),
                "trust_net": rec.get("invest_lots"), "dealer_net": rec.get("dealer_lots"),
                "total_net": rec.get("total_lots"),
                "consecutive_days": _signed_streak(total_series),
                "foreign_days": f_days, "trust_days": t_days, "dealer_days": d_days}

    # 最後備援：官方端點暫時無回應時才使用 FinMind。
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
    dates = sorted(by_date)                       # 舊→新
    d = dates[-1]
    b = by_date[d]
    f = (b.get("Foreign_Investor", 0) + b.get("Foreign_Dealer_Self", 0)) // 1000
    t = b.get("Investment_Trust", 0) // 1000
    dl = (b.get("Dealer_self", 0) + b.get("Dealer_Hedging", 0)) // 1000
    # 三法人各自的每日淨額序列(股,sign 用即可) → 帶號連續天數
    f_series = [(by_date[dt].get("Foreign_Investor", 0)
                 + by_date[dt].get("Foreign_Dealer_Self", 0)) for dt in dates]
    t_series = [by_date[dt].get("Investment_Trust", 0) for dt in dates]
    dl_series = [(by_date[dt].get("Dealer_self", 0)
                  + by_date[dt].get("Dealer_Hedging", 0)) for dt in dates]
    return {"date": d, "foreign_net": f, "trust_net": t, "dealer_net": dl,
            "total_net": f + t + dl,
            "foreign_days": _signed_streak(f_series),
            "trust_days": _signed_streak(t_series),
            "dealer_days": _signed_streak(dl_series)}


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
    prows = official_price.fetch(code, d)  # twstock 官方日 K, old→new
    price_source = "twstock"
    if not prows:
        prows = dc.fetch_finmind_price(code, days=90)  # 最後備援
        price_source = "finmind_fallback"
    prows = [r for r in prows if r["date"] <= dd]
    if not prows or len(prows) < 2:
        return {}
    closes = [r["close"] for r in prows]
    vols = [r["Trading_Volume"] for r in prows]
    last = prows[-1]
    close = last["close"]
    prev = closes[-2]
    chg = round((close - prev) / prev * 100, 2) if prev else 0.0

    last20 = closes[-20:]
    ma20 = _mean(last20) or close
    tech = {
        "ma20": round(ma20, 2), "above_ma20": close > ma20,
        "bias": round((close - ma20) / ma20 * 100, 2) if ma20 else 0,
        "trigger": round(min(max(r.get("max") or close for r in prows[-10:]), close * 1.05), 2),
    }
    inst = analyst._inst_20d(code)
    brk = analyst._inst_breakdown(code)
    mgn = analyst._margin_trend(code)
    tinst = _today_inst(code, dd)

    now = _dt.datetime.now().isoformat(timespec="seconds")
    out: dict = {}

    # daily_bar(死值 · INSERT OR IGNORE:寫壞了事後蓋不掉,所以「當下就不能寫壞」)
    #
    # 護欄 — 只寫「已定案的真收盤」,擋兩種髒資料:
    #   (1) 過時:FinMind 最新一筆日期 < 資料日 → 當日 EOD 還沒出,若照寫等於把
    #       前一日 OHLC 貼上今天標籤(過時假 bar)。
    #   (2) 退化預備價:當日那筆 open=high=low=close 單點(收盤剛過、FinMind 尚未
    #       定案時常回漲停/試撮參考價)。這種和「真鎖漲停」在 OHLC 上無法區分,
    #       故只在 EOD 定案前(台灣 <14:30)的當日即時跑才視為預備價跳過;
    #       定案後(排程 14:40 才跑)或歷史補跑(帶 date)一律信任,真鎖漲停照收。
    o_, hi_, lo_ = last.get("open"), last.get("max"), last.get("min")
    _tw_hour = (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).hour
    _live_today = (dd == today_tw().isoformat())          # 非歷史補跑
    _degenerate = (o_ is not None and o_ == hi_ == lo_ == close)
    _preliminary = _degenerate and _live_today and _tw_hour < 14  # 定案前的退化單點價

    if last["date"] == dd and not _preliminary:
        out["daily_bar"] = {
            "code": code, "data_date": dd,
            "open": o_, "high": hi_, "low": lo_,
            "close": close, "volume": last.get("Trading_Volume"),
            "ma5": round(_mean(closes[-5:]) or close, 2),
            "ma20": tech["ma20"],
            "ma60": round(_mean(closes[-60:]) or close, 2),
            "vol_ma20": int(_mean(vols[-20:]) or 0),
            "source": price_source, "fetched_at": now,
        }
    else:
        _why = "預備/退化單點價,待定案後補" if _preliminary else f"FinMind 最新僅到 {last['date']},無 {dd} 收盤"
        print(f"  ⚠ {code}: 跳過 daily_bar({_why})")

    # inst_flow(死值)— 缺就不寫這張,screener 標 NO_DATA
    #
    # 護欄(2026-08-05):法人官方資料 ~15:00–16:00 才公布,collect 14:40 跑時最新常
    # 只到「前一交易日」。_today_inst 會回那筆舊資料(其 date < dd);若照寫 = 把前一日
    # 法人數字＋連買連賣天數蓋上今天標籤,且 inst_flow 不可變、事後永遠蓋不掉
    # (3363 08-04 被寫成 08-03「連買3日」實際賣超的病灶)。故只在「資料實際日 == 資料日」
    # 才寫,否則跳過標 NO_DATA,等公布後補跑。streak(consecutive_days/*_days)同源一起擋。
    if tinst and tinst.get("date") == dd:
        out["inst_flow"] = {
            "code": code, "data_date": dd,
            "foreign_net": tinst["foreign_net"], "trust_net": tinst["trust_net"],
            "dealer_net": tinst["dealer_net"], "total_net": tinst["total_net"],
            "consecutive_days": tinst.get("consecutive_days", inst["streak"]),
            # 三法人各自帶號連續天數(正連買/負連賣) — L3 才真正吃到。inst_flow 有
            # immutable trigger,收盤後補不了,故一定要在寫入當下就算好(2026-08-04)。
            "foreign_days": tinst["foreign_days"], "trust_days": tinst["trust_days"],
            "dealer_days": tinst["dealer_days"],
            "source": "TWSE T86 / TPEx 官方法人", "fetched_at": now,
        }
    elif tinst:
        print(f"  ⚠ {code}: 跳過 inst_flow(法人最新僅到 {tinst.get('date')},無 {dd} 蓋章,待公布後補)")

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
