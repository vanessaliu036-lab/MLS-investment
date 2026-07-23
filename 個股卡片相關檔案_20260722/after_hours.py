"""
MLS 標準版 — after_hours.py
盤後複查(15:05 排程):
  ① 收盤驗證:比對今日觀察清單 vs 實際訊號,算命中率,找遺漏股
  ② 抗跌股篩選 → 產出「明日觀察清單」(資金流出族群中的逆勢股)
  ③ 寫入 SQLite + 同步 Airtable(未設 token 則跳過,系統照常)
  ④ Telegram 摘要

Airtable 環境變數(選用):
    AIRTABLE_TOKEN / AIRTABLE_BASE_ID
資料表:Daily_Watchlist / Review_Log(欄位見交接規格書 v2 §3.3)
"""

import os
import json
import urllib.request
from datetime import datetime, timedelta, timezone

import config as C
import broker
import chips
import db
import notifier

TW_TZ = timezone(timedelta(hours=8))


# ══════════════════════════════════════════════════════
# ⓪ ABAB 輪動 × 資金價量四象限分析(使用者觀察定案)
# ══════════════════════════════════════════════════════
# 象限定義(收盤後,資金方向=成交佔比 vs 前一交易日):
#   in_up    資金流入+群組漲  = 最健康,順勢主做
#   in_down  資金流入+群組跌  = 邊拉邊賣/假紅出貨疑慮(交接檔鐵律2/8)
#   out_down 資金流出+群組跌  = 輪動休息日;若 ABAB 成立=B日,
#            明日按節奏偏反彈,但個股仍須大戶連買/法人未斷才列觀察
#   out_up   資金流出+群組漲  = 量縮惜售,續航存疑
#
# ABAB 判定:該族群最近4個交易日中位漲幅正負交錯(|pct|>0.5%),
#           今日為 B(跌)日 → 明日偏 A(漲)日。
#           鐵律:單日不下結論;ABAB 只給「節奏傾向」,個股健康度另判。

ABAB_MIN_ABS = 0.5      # 交錯判定的最小單日幅度(%)


def _flow_dir(sector_name, today_share):
    prev = db.prev_amount_share(sector_name)
    if prev is None:
        return 1 if today_share > 0 else -1     # 首日以佔比正負暫代
    return 1 if today_share >= prev else -1


def _quadrant(flow_dir, pct):
    if flow_dir > 0 and pct >= 0:  return "in_up"
    if flow_dir > 0 and pct < 0:   return "in_down"
    if flow_dir < 0 and pct < 0:   return "out_down"
    return "out_up"


def _is_abab(history, today_pct):
    """history: 舊→新的 sector_daily(不含今日)。與今日合併判交錯。"""
    pcts = [h["pct"] for h in history][-3:] + [today_pct]
    if len(pcts) < 4:
        return False
    if any(abs(p) < ABAB_MIN_ABS for p in pcts):
        return False
    signs = [1 if p > 0 else -1 for p in pcts]
    return all(signs[i] != signs[i + 1] for i in range(3))


QUADRANT_ADVICE = {
    "in_up":    "資金流入且群組上漲=最健康象限,明日順勢主做,個股沿突破/站均線訊號進,破均價即出。",
    "in_down":  "⚠️ 資金欄流入但群組收跌=邊拉邊賣/假紅出貨疑慮(鐵律:急殺時資金欄假紅=賣壓被算成主動買)。"
                "明日不搶反彈、不用資金欄找接盤;一律等收盤外資蓋章,群組內強勢股也防漲完隔天倒。",
    "out_down": "資金流出且群組下跌=輪動休息日。僅將『大戶/法人連買未斷、今日僅獲利了結洗盤』之個股列明日觀察;其餘不接刀。",
    "out_up":   "資金流出但群組收漲=量縮惜售,續航存疑。不加碼,持有者沿5MA防守。",
}


def rotation_analysis(sectors, snaps):
    """
    對每個攻擊部隊族群產出:象限、ABAB狀態、AI建議、
    以及 out_down/in_down 族群內的「大戶連買抗跌股」清單。
    回傳 (sector_reports, resilient_picks)
    """
    tdate = db.today()
    reports, resilient, daily_rows = [], [], []

    by_sector = {}
    for s in snaps:
        if s.get("sector"):
            by_sector.setdefault(s["sector"], []).append(s)

    for sec in sectors:
        if sec["type"] != "attack":
            continue
        name = sec["name"]
        fdir = _flow_dir(name, sec["amount_share"])
        quad = _quadrant(fdir, sec["pct"])
        hist = db.sector_history(name, days=5)
        abab = _is_abab(hist, sec["pct"])

        advice = QUADRANT_ADVICE[quad]
        if abab and quad in ("out_down", "in_down"):
            advice = ("ABAB 節奏成立:今日為 B(跌)日,按近四日輪動明日偏 A(漲)日。"
                      "但節奏≠個股健康——僅追蹤下列大戶連買個股,其餘照象限紀律。 ") + advice
        elif abab and quad in ("in_up", "out_up"):
            advice = ("ABAB 節奏成立:今日為 A(漲)日,按節奏明日偏 B(休息)日,"
                      "追高需防隔日回落,以短打處理。 ") + advice

        # 落難族群裡抓「大戶連買、僅被獲利了結」的抗跌股
        picks = []
        if quad in ("out_down", "in_down"):
            eng = getattr(C, "ENGINE_STOCKS", set())
            for m in by_sector.get(name, []):
                if m["code"] in eng:
                    continue
                ch = chips.get_chips(m["code"])
                inst_ok = (ch["inst_net_20d_lots"] or 0) > 0 or (ch["inst_streak"] or 0) >= 3
                big_ok = (ch["big_holder_trend"] is None) or ch["big_holder_trend"] >= -0.2
                if not (inst_ok and big_ok):
                    continue
                mild_drop = m["change_rate"] > sec["pct"]          # 相對族群抗跌
                strong_chip = (ch["inst_streak"] or 0) >= 3 or \
                              (ch["big_holder_trend"] or 0) > 0     # 籌碼強勢未斷
                # 使用者定義:大戶連買、今日僅被獲利了結(跌更深也算)
                if mild_drop or strong_chip:
                    kind = "相對抗跌" if mild_drop else "獲利了結洗盤"
                    reason = (f"{quad}:{'ABAB-B日 ' if abab else ''}{kind} "
                              f"大戶/法人未斷(近月{ch['inst_net_20d_lots'] or 0:+,}張"
                              f"{',連買'+str(ch['inst_streak'])+'日' if (ch['inst_streak'] or 0)>=3 else ''}) "
                              f"{m['change_rate']:+.1f}% vs 族群{sec['pct']:+.1f}%")
                    picks.append({"code": m["code"],
                                  "name": C.NAME_MAP.get(m["code"], m["code"]),
                                  "sector": name, "reason": reason})
        resilient.extend(picks)

        reports.append({"sector": name, "quadrant": quad, "abab": abab,
                        "pct": sec["pct"], "flow_dir": fdir,
                        "advice": advice,
                        "resilient": [p["code"] for p in picks]})
        daily_rows.append({"sector": name, "pct": sec["pct"],
                           "amount_share": sec["amount_share"],
                           "flow_dir": fdir, "quadrant": quad})

    db.save_sector_daily(tdate, daily_rows)
    return reports, resilient


# ══════════════════════════════════════════════════════
# ① 收盤驗證
# ══════════════════════════════════════════════════════
def verify_today(today_signals_codes, strong_codes):
    """
    today_signals_codes: 今日有 buy/watch 訊號的股票集合
    strong_codes:        今日盤中強勢(漲>2%且量比>1.5)的股票集合
    """
    tdate = db.today()
    wl = db.load_watchlist(tdate)
    hit = 0
    for w in wl:
        if w["stock_id"] in today_signals_codes or w["stock_id"] in strong_codes:
            db.mark_watch_hit(tdate, w["stock_id"])
            hit += 1
    missed = sorted(strong_codes - {w["stock_id"] for w in wl})
    rate = db.write_review(tdate, len(wl), hit, missed,
                           notes="自動收盤驗證")
    return {"date": tdate, "total": len(wl), "hit": hit,
            "rate": rate, "missed": missed}


# ══════════════════════════════════════════════════════
# ② 抗跌股篩選 → 明日觀察清單
# ══════════════════════════════════════════════════════
def build_tomorrow_watchlist(sectors, snaps):
    """
    規格書 §盤後複查:在「資金流出族群」(flow_score<0 或 pct<0)中,
    篩逆勢抗跌股:
      · 個股漲幅 > 族群中位 + 1.5pp(逆勢)
      · 量比 >= 0.8(未明顯量縮)
      · 法人近月買超 > 0(chips 快取,免額外請求)
    """
    out_sectors = {s["name"] for s in sectors
                   if s["type"] == "attack" and (s["flow_score"] < 0 or s["pct"] < 0)}
    rows = []
    for s in snaps:
        sec = s.get("sector")
        if sec not in out_sectors:
            continue
        sec_pct = next(x["pct"] for x in sectors if x["name"] == sec)
        if s["change_rate"] < sec_pct + 1.5:
            continue
        if (s["volume_ratio"] or 0) < 0.8:
            continue
        ch = chips.get_chips(s["code"])
        if ch["inst_net_20d_lots"] is not None and ch["inst_net_20d_lots"] <= 0:
            continue
        rows.append({
            "code": s["code"],
            "name": C.NAME_MAP.get(s["code"], s["code"]),
            "sector": sec,
            "reason": f"資金流出族群抗跌 逆勢{s['change_rate']:+.1f}% 量比{s['volume_ratio']:.1f}"
                      + (f" 法人買超{ch['inst_net_20d_lots']:,}張"
                         if ch["inst_net_20d_lots"] else ""),
        })
    return rows[:10]   # 上限10檔


def next_trade_date():
    d = datetime.now(TW_TZ)
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════
# ③ Airtable 同步(選用)
# ══════════════════════════════════════════════════════
def _airtable_post(table, records):
    token = os.environ.get("AIRTABLE_TOKEN", "")
    base = os.environ.get("AIRTABLE_BASE_ID", "")
    if not token or not base:
        print(f"[airtable/skip] 未設定 token,{table} {len(records)} 筆僅存本地")
        return False
    try:
        url = f"https://api.airtable.com/v0/{base}/{urllib.parse.quote(table)}"
        for i in range(0, len(records), 10):     # Airtable 每次上限10筆
            body = json.dumps({"records": [{"fields": r} for r in records[i:i+10]]}).encode()
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        print(f"[airtable] {table} 同步失敗: {e}")
        return False


import urllib.parse  # noqa: E402  (供 _airtable_post 使用)


# ══════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════
def run(last_state):
    """
    last_state: 收盤前最後一輪 engine.build_state() 的結果 + 原始快照
                需含 keys: sectors(含members不需要), stocks, _snaps(原始快照)
    """
    snaps = last_state.get("_snaps", [])
    sectors = last_state.get("_sectors_full", last_state.get("sectors", []))

    # ① 收盤驗證
    sig_codes = {x["code"] for x in last_state.get("stocks", [])
                 if x["action"] in ("buy", "watch")}
    strong = {s["code"] for s in snaps
              if s.get("change_rate", 0) > 2 and (s.get("volume_ratio") or 0) > 1.5}
    review = verify_today(sig_codes, strong)

    # ⓪ ABAB 四象限輪動分析(使用者觀察定案)
    rotation_reports, resilient = rotation_analysis(sectors, snaps)

    # ② 明日觀察清單 = 原抗跌篩選 ∪ 輪動分析抗跌股(去重,輪動優先)
    tomorrow = next_trade_date()
    wl = build_tomorrow_watchlist(sectors, snaps)
    seen = {w["code"] for w in resilient}
    wl = resilient + [w for w in wl if w["code"] not in seen]
    wl = wl[:10]
    db.save_watchlist(tomorrow, wl)

    # ③ Airtable
    _airtable_post("Review_Log", [{
        "Date": review["date"], "Watch_Total": review["total"],
        "Watch_Hit": review["hit"], "Hit_Rate": review["rate"],
        "Missed_Stocks": json.dumps(review["missed"], ensure_ascii=False),
    }])
    _airtable_post("Daily_Watchlist", [{
        "Date": tomorrow, "Stock_ID": w["code"], "Stock_Name": w["name"],
        "Sector": w["sector"], "Reason": w["reason"],
    } for w in wl])

    # ⑤ 因子權重自學習:今日進場訊號 → 收盤成敗 → 30日權重更新
    FACTOR_HIT = {"trend": 10, "volume": 10, "rs": 8, "chip": 10, "sector": 8}
    close_map = {s["code"]: s["price"] for s in snaps}
    frows = {}
    for sig in db.today_buy_signals():
        cl = close_map.get(sig["stock_id"])
        if cl is None or not sig.get("price"):
            continue
        ok = cl > sig["price"] * 1.003          # 收盤高於訊號價0.3%=成功
        try:
            fs = json.loads(sig.get("factors") or "{}")
        except Exception:
            fs = {}
        for f, thr in FACTOR_HIT.items():
            if (fs.get(f) or 0) >= thr:          # 該因子有實質貢獻才計
                r = frows.setdefault(f, {"factor": f, "triggered": 0, "success": 0})
                r["triggered"] += 1
                r["success"] += 1 if ok else 0
    if frows:
        db.record_factor_stats(list(frows.values()))
    new_w = db.update_factor_weights(days=30)

    # ⑥ 80%準度控制器:記錄訊號成敗 → rolling精度 → 調整進場門檻
    outcomes = []
    for sig in db.today_buy_signals():
        cl = close_map.get(sig["stock_id"])
        if cl is None or not sig.get("price"):
            continue
        outcomes.append({"stock_id": sig["stock_id"],
                         "signal_price": sig["price"], "close_price": cl,
                         "success": cl > sig["price"] * 1.003})
    if outcomes:
        db.record_outcomes(outcomes)
    prec, n = db.rolling_precision(days=10)
    thr = float(db.kv_get("entry_score_min", 40))
    if prec is not None and n >= 10:            # 樣本足才調
        if prec < 0.80:
            thr = min(70, thr + 3)              # 收緊:寧缺勿濫
        elif prec > 0.85:
            thr = max(35, thr - 2)              # 放寬:恢復進攻
        db.kv_set("entry_score_min", thr)
    precision_report = {"rolling_precision": None if prec is None else round(prec, 3),
                        "samples": n, "entry_score_min": thr}

    # ④ Telegram 摘要(含四象限/ABAB 輪動報告)
    stats = db.today_stats()
    QN = {"in_up": "流入↗漲", "in_down": "流入↗跌⚠邊拉邊賣",
          "out_down": "流出↘跌·休息", "out_up": "流出↘漲·量縮"}
    rot_lines = []
    for r in sorted(rotation_reports, key=lambda x: x["pct"], reverse=True):
        tag = " [ABAB]" if r["abab"] else ""
        res = f" 抗跌:{','.join(r['resilient'])}" if r["resilient"] else ""
        rot_lines.append(f"{r['sector']} {r['pct']:+.1f}% {QN[r['quadrant']]}{tag}{res}")
    notifier.push_summary(
        f"📋 *盤後複查* {review['date']}\n"
        f"觀察清單命中率 *{review['rate']}%* ({review['hit']}/{review['total']})\n"
        f"遺漏 {len(review['missed'])} 檔:{'、'.join(review['missed'][:5]) or '無'}\n"
        f"今日訊號 {stats.get('total', 0)}(進場 {stats.get('buys', 0)} / 風險 {stats.get('risks', 0)})\n"
        f"— 族群四象限 —\n" + "\n".join(rot_lines) + "\n"
        f"精度 {precision_report['rolling_precision'] if precision_report['rolling_precision'] is not None else '—'}"
        f"(n={precision_report['samples']}) 門檻→{precision_report['entry_score_min']:.0f}\n"
        f"明日觀察清單 *{len(wl)} 檔* 已產出({tomorrow})")
    # ── 插件掛鉤:NEXORA 盤後報告(失敗不影響主流程) ──
    nexora_out = None
    try:
        import nexora
        nexora_out = nexora.run_report(last_state, rotation_reports)
        notifier.push_summary(nexora_out["summary"])
    except Exception as e:
        print(f"[plugin/nexora] 跳過:{e}")

    # ── 插件掛鉤:EOD 數據驗證×訓練管線(失敗不影響主流程) ──
    eod_out = None
    try:
        import eod_pipeline
        eod_out = eod_pipeline.run(last_state, sectors=sectors,
                                   notify=notifier.push_summary)
    except Exception as e:
        print(f"[plugin/eod] 跳過:{e}")

    # ── 插件掛鉤:李佛摩六欄紀錄(盤後選股中心,每日15:00後存檔) ──
    livermore_out = None
    try:
        import livermore
        livermore_out = livermore.record_today()       # 六欄紀錄落地 mls.db
        sp = livermore.six_point_scan()                # 六點轉向:盤後選股中心
        livermore_out["sixpoint_qualified"] = len(sp["qualified"])
        notifier.push_summary(
            f"📈 李佛摩已存 {livermore_out.get('date')} · "
            f"{livermore_out.get('saved', 0)} 檔｜六點合格 {len(sp['qualified'])} 檔"
            f"(頁面 /livermore)")
    except Exception as e:
        print(f"[plugin/livermore] 跳過:{e}")

    # ── 插件掛鉤:引擎角色週審查(每週五;跟著主流輪替,v3.0) ──
    try:
        from datetime import datetime as _dt
        if _dt.now().weekday() == 4:
            import engine_review
            rev = engine_review.review()
            notifier.push_summary(engine_review.summary_text(rev))
    except Exception as e:
        print(f"[plugin/engine_review] 跳過:{e}")

    # ── 插件掛鉤:MLS 資金決策 v2.2(觀察→驗證→勝率統計閉環) ──
    decision_out = None
    try:
        import decision_v22
        decision_out = decision_v22.run_report(last_state)
        notifier.push_summary(decision_out["summary"])
    except Exception as e:
        print(f"[plugin/decision] 跳過:{e}")

    return {"review": review, "tomorrow_watchlist": wl,
            "rotation": rotation_reports, "new_weights": new_w,
            "precision": precision_report, "nexora": nexora_out,
            "eod": eod_out, "livermore": livermore_out,
            "decision": decision_out}


# ══════════════════════════════════════════════════════
# 08:55 開盤重驗(用試撮/開盤快照)
# ══════════════════════════════════════════════════════
def reverify_watchlist():
    """
    對今日觀察清單抓快照:跳空跌破昨低(low>price 開盤即弱)或跌>2% → 降級。
    """
    tdate = db.today()
    wl = db.load_watchlist(tdate)
    if not wl:
        return []
    snaps = {s["code"]: s for s in broker.batch_snapshots([w["stock_id"] for w in wl])}
    demoted = []
    for w in wl:
        s = snaps.get(w["stock_id"])
        bad = bool(s and (s["change_rate"] < -2))
        db.mark_reverify(tdate, w["stock_id"], bad)
        if bad:
            demoted.append(w["stock_id"])
    if demoted:
        notifier.push_summary(
            f"⚠️ *開盤重驗* 觀察清單降級 {len(demoted)} 檔:{'、'.join(demoted)}(跳空轉弱)")
    return demoted
