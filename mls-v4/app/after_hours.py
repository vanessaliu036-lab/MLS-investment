"""
MLS v4.0 — after_hours.py
盤後流水線（15:05 排程）：
  ① 蓋章：全池重算五模組 → dec_health（eod_final）
  ② 象限時序：health_daily（連續/趨勢/象限歷史）
  ③ 李佛摩六欄落地
  ④ 抗跌股雷達：落難族群逆勢股
  ⑤ 觀察清單：漏斗四關 → 明日標的
  ⑥ 隔日驗證：命中率累積
  ⑦ 族群象限 + ABAB
"""
import config as C
import db
import broker
import chips
import data_collector
import decision
import funnel
import livermore

QUAD_ADVICE = {
    "in_up": "資金流入且族群漲=最健康，順勢主做，沿突破/站均線進，破均價出。",
    "in_down": "資金流入但族群跌=邊拉邊賣/假紅出貨疑慮，不搶反彈、等收盤外資蓋章。",
    "out_down": "資金流出且族群跌=輪動休息日，僅追蹤大戶連買、僅獲利了結洗盤之抗跌股。",
    "out_up": "資金流出但族群漲=量縮惜售，續航存疑，不加碼沿5MA防守。",
}


def _sector_pct(snaps):
    """族群漲跌 = 成分股平均。"""
    by = {}
    for s in snaps:
        by.setdefault(s["sector"], []).append(s["change_rate"])
    return {k: round(sum(v) / len(v), 2) for k, v in by.items()}


def _market_pct(snaps):
    """大盤 = 全池加權平均（demo 用簡單平均近似）。"""
    if not snaps:
        return 0.0
    return round(sum(s["change_rate"] for s in snaps) / len(snaps), 2)


def run_eod(trade_date=None):
    """盤後主流程。回傳摘要。"""
    trade_date = trade_date or db.today()
    broker.connect()

    # ① 確保當日 TWSE/TPEx 三大法人已落 DB(沒抓就抓,失敗不阻擋後續)
    ymd = trade_date.replace("-", "")
    try:
        snap_result = data_collector.fetch_today_all_to_db(ymd)
        print(f"[EOD] inst_count={snap_result.get('inst_count')} price_twse={snap_result.get('price_twse')} price_tpex={snap_result.get('price_tpex')}")
    except Exception as e:
        print(f"[EOD] ⚠️ 抓當日法人失敗,後續 chips 會走 demo/db cache:{e}")

    snaps = broker.snapshot()
    sector_pct = _sector_pct(snaps)
    market_pct = _market_pct(snaps)

    evals = []
    for s in snaps:
        chip = chips.get_chips(s["code"])
        ma20 = broker.ma20_of(s["code"])
        prev = db.prev_health_daily(s["code"], trade_date)
        prev_quad = prev["quadrant"] if prev else None
        prev_streak = prev["flow_streak"] if prev else 0
        liv_prev = None
        ev = decision.evaluate_stock(
            s, chip, sector_pct.get(s["sector"], 0), market_pct, ma20,
            prev_quad=prev_quad, prev_streak=prev_streak, liv=liv_prev)
        evals.append(ev)

    # ① 蓋章 dec_health
    db.save_dec_health(trade_date, evals)
    # ② 象限時序
    for ev in evals:
        db.save_health_daily(trade_date, ev["code"], ev["quad"],
                             ev["score"], ev["streak"], ev["trend"])
    # ③ 李佛摩
    livermore.record_daily(trade_date, evals)
    # ④+⑤ 漏斗 → 觀察清單
    fn = funnel.run_funnel(evals)
    passed = fn["passed"]
    wl_rows = []
    for ev in passed[:C.WATCHLIST_MAX]:
        wl_rows.append({
            "code": ev["code"], "name": ev["name"], "sector": ev["sector"],
            "track": ev["track"], "grade": ev["grade"], "score": ev["score"],
            "quad": ev["quad"], "close": ev["close"], "chg": ev["chg"],
            "vr": ev["vr"], "foreign_lots": ev["foreign_lots"],
            "invest_lots": ev["invest_lots"],
            "big_holder": f"{ev['big_holder_trend']:+.1f}pp" if ev["big_holder_trend"] is not None else "—",
            "obs_high": ev["high"],
            "reason": ev["chip_note"],
        })
    target_date = _next_trade_date(trade_date)
    db.save_watchlist(trade_date, target_date, wl_rows)

    # ⑥ 隔日驗證（驗證昨日清單 vs 今日實際）
    verify_summary = _verify_prev(trade_date, evals)

    # ⑦ 族群象限
    sector_rows = []
    for sec, pct in sector_pct.items():
        prev_share = db.prev_amount_share(sec)
        share = pct  # demo：以漲跌近似資金佔比方向
        fdir = 1 if (prev_share is None and share > 0) or \
                    (prev_share is not None and share >= prev_share) else -1
        quad = decision.quadrant(1 if fdir > 0 else -1, pct)
        sector_rows.append({"sector": sec, "pct": pct, "amount_share": share,
                            "flow_dir": fdir, "quadrant": quad})
    db.save_sector_daily(trade_date, sector_rows)

    return {
        "date": trade_date,
        "total": len(evals),
        "ready": sum(1 for e in evals if e["grade"] == "Ready"),
        "watchlist": len(wl_rows),
        "funnel": {"universe": fn["universe"],
                   "stages": [{"gate": s["gate"], "out": s["out"]} for s in fn["stages"]]},
        "verify": verify_summary,
    }


def _next_trade_date(d):
    from datetime import datetime, timedelta
    dt = datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)
    while dt.weekday() >= 5:
        dt += timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def _verify_prev(trade_date, today_evals):
    """驗證：昨日清單的 target_date=今日者，用今日實際表現判定觸發/命中。"""
    wl = db.load_watchlist(trade_date)
    if not wl:
        return {"verified": 0, "hit": 0, "rate": None}
    ev_map = {e["code"]: e for e in today_evals}
    rows, hit = [], 0
    for w in wl:
        ev = ev_map.get(w["code"])
        if not ev:
            continue
        obs_high = w.get("obs_high") or 0
        triggered = 1 if ev["high"] > obs_high else 0
        next_high_pct = round((ev["high"] - w["close"]) / w["close"] * 100, 2) if w["close"] else 0
        next_close_pct = round(ev["chg"], 2)
        # 達標：攻擊軌隔日收盤≥SUCCESS_PCT；引擎軌 5日≥+1%（此處以隔日近似，串接後可延長）
        success = 1 if next_close_pct >= C.SUCCESS_PCT else 0
        hit += success
        rows.append({
            "target_date": trade_date, "code": w["code"],
            "grade": w["grade"], "track": w["track"],
            "triggered": triggered, "entered": triggered,
            "success": success, "next_high_pct": next_high_pct,
            "next_close_pct": next_close_pct, "hold_days": 1,
            "hold_ret_pct": next_close_pct,
        })
    db.save_verify(rows)
    rate = round(hit / len(rows) * 100, 1) if rows else None
    return {"verified": len(rows), "hit": hit, "rate": rate}


# ── 抗跌股雷達 ──
def resilient_radar(evals):
    """落難族群(in_down/out_down)中，法人未斷+逆勢的抗跌股。"""
    picks = []
    for ev in evals:
        if ev["quad"] not in ("in_down", "out_down"):
            continue
        if ev["vs_sector"] <= 0:
            continue
        net = ev.get("inst_net_20d") or 0
        streak = ev.get("inst_streak") or 0
        trend = ev.get("big_holder_trend")
        if not (net > 0 or streak >= 3 or (trend is not None and trend > -0.2)):
            continue
        picks.append(ev)
    return picks


# ── 統計驗證 ──
def stats(days=None):
    days = days or C.STATS_DAYS
    from datetime import datetime, timedelta
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = {"tracks": [], "quadrants": [], "buckets": []}
    with db._lock, db._conn() as c:
        # 分軌勝率
        for tk, tkn in (("engine", "引擎軌(波段)"), ("attack", "攻擊軌(短線)")):
            for g in ("Ready", "Watch"):
                r = c.execute("""SELECT COUNT(*) n, SUM(success) s,
                     AVG(next_close_pct) ac, AVG(hold_ret_pct) hr
                   FROM dec_verify WHERE track=? AND grade=? AND target_date>=?""",
                   (tk, g, since)).fetchone()
                n = r["n"] or 0
                if n == 0:
                    continue
                out["tracks"].append({
                    "track": tk, "name": f"{tkn}·{g}", "total": n,
                    "hit_rate": round((r["s"] or 0) / n * 100, 1),
                    "avg_ret": round(r["ac"], 2) if r["ac"] is not None else None,
                })
        # 四象限隔日
        rows = c.execute("""SELECT h.quad q, COUNT(*) n, AVG(n2.chg) avg_next,
                 SUM(CASE WHEN n2.chg>=? THEN 1 ELSE 0 END) win
              FROM dec_health h JOIN dec_health n2
                ON n2.code=h.code AND n2.trade_date=(
                  SELECT MIN(trade_date) FROM dec_health
                  WHERE code=h.code AND trade_date>h.trade_date)
              WHERE h.trade_date>=? GROUP BY h.quad""",
              (C.SUCCESS_PCT, since)).fetchall()
        for r in rows:
            out["quadrants"].append({
                "quad": r["q"], "name": decision.QUAD_NAME.get(r["q"], r["q"]),
                "total": r["n"],
                "avg_next": round(r["avg_next"], 2) if r["avg_next"] is not None else None,
                "win_rate": round((r["win"] or 0) / r["n"] * 100, 1) if r["n"] else None,
            })
        # 分數區間
        for lo, hi in ((90, 100), (80, 89), (70, 79), (65, 69), (50, 64)):
            r = c.execute("""SELECT COUNT(*) n, SUM(v.success) s, AVG(v.next_close_pct) ac
                FROM dec_verify v JOIN dec_watchlist w
                  ON w.target_date=v.target_date AND w.code=v.code
                WHERE w.score BETWEEN ? AND ? AND v.target_date>=?""",
                (lo, hi, since)).fetchone()
            n = r["n"] or 0
            out["buckets"].append({
                "bucket": f"{lo}–{hi}", "total": n,
                "hit_rate": round((r["s"] or 0) / n * 100, 1) if n else None,
                "avg_next": round(r["ac"], 2) if r["ac"] is not None else None,
            })
    return out
