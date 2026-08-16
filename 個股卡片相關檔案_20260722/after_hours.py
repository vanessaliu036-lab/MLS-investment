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
import statistics
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config as C
import broker
import chips
import db
import notifier
import signal_pattern

TW_TZ = timezone(timedelta(hours=8))

# 盤中 radar 收盤狀態（含 group/score/sector）由 vps_intraday_test 寫在 repo 根。
# build_tomorrow_watchlist 讀它取「今日可操作/觀察存活者」排序名單。
RADAR_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "intraday_live_snapshot.json"


# ══════════════════════════════════════════════════════
# Phase 3/4 純函式（可單測；讀檔/判定/選股皆無副作用）
# ══════════════════════════════════════════════════════
def _radar_snapshot_rows(trade_date):
    """讀 radar 收盤快照，只回「當日」的 rows；日期不符或讀不到回 []。
    絕不 raise —— 任何異常都回 [] 讓呼叫端回退純抗跌名單。"""
    try:
        if not RADAR_SNAPSHOT_PATH.exists():
            return []
        payload = json.loads(RADAR_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if payload.get("trade_date") != trade_date:
            return []
        rows = (payload.get("result") or {}).get("rows") or []
        return [r for r in rows if isinstance(r, dict) and r.get("code")]
    except Exception as exc:
        print(f"[after_hours] radar 快照讀取失敗，回退純抗跌：{exc}")
        return []


def _sector_medians(radar_rows):
    """從 radar 快照 rows 算各族群中位漲幅（收盤驗證缺 last_state.sectors 時用）。
    回傳 [{name, pct}]，與 verify_today 只用 name/pct 相容。"""
    by = {}
    for r in radar_rows:
        cr, sec = r.get("change_rate"), r.get("sector")
        if cr is not None and sec:
            by.setdefault(sec, []).append(cr)
    return {sec: round(statistics.median(v), 2) for sec, v in by.items()}


def select_radar_watchlist(radar_rows, resilient_rows, limit=10):
    """名單來源 = Radar 優先、Resilient 補足（不 union）。純函式。

    radar_rows: radar 快照 rows（含 group/score/sector/price/change_rate）。
    resilient_rows: 既有抗跌候選（已帶 source='resilient'）。
    回傳 (picks, radar_rejects)。picks 每筆帶 source/entry_ref/factor_score/
    group_at_pick，供 save_watchlist 與 T+1 分流驗證使用。
    """
    def _score(r):
        return r.get("score") or 0

    actionable = sorted((r for r in radar_rows if r.get("group") == "可操作"),
                        key=_score, reverse=True)
    observe = sorted((r for r in radar_rows if r.get("group") == "觀察"),
                     key=_score, reverse=True)

    picks, seen = [], set()
    for r in actionable + observe:      # 可操作優先，再觀察，各自分數高者先
        if len(picks) >= limit:
            break
        code = str(r.get("code"))
        if code in seen:
            continue
        seen.add(code)
        picks.append({
            "code": code, "name": r.get("name") or code,
            "sector": r.get("sector"), "source": "radar",
            "entry_ref": r.get("price"),          # 進場基準＝選股日收盤價
            "factor_score": r.get("score"),
            "group_at_pick": r.get("group"),
            "reason": (r.get("reason")
                       or f"{r.get('group')} 七因子 {r.get('score')}／100"),
        })

    # 不足 limit 才用抗跌補足（去重）
    for w in resilient_rows:
        if len(picks) >= limit:
            break
        code = str(w.get("code"))
        if code in seen:
            continue
        seen.add(code)
        picks.append(w)

    # radar 落選＝觀察/排除中未入選者，帶逐因子分數留痕
    radar_rejects = []
    for r in radar_rows:
        code = str(r.get("code"))
        if code in seen or r.get("group") == "可操作":
            continue
        rej = {
            "code": code, "name": r.get("name") or code,
            "sector": r.get("sector"), "source": "radar",
            "factor_score": r.get("score"), "score_total": r.get("score"),
            "fail_factor": ("排除" if r.get("group") == "排除"
                            else "觀察未達門檻/名額不足"),
            "detail": (r.get("subgroup") or "")
                      + (f"（{r.get('score_pct')}%）" if r.get("score_pct") is not None else ""),
        }
        rej.update(_reject_factor_scores(r.get("score_factors")))
        radar_rejects.append(rej)
    return picks, radar_rejects


# radar 七因子 → watch_reject 具名欄的「真正對得上」對應（其餘留 NULL）。
_FACTOR_COL = {"net_active": "score_volume", "vs_ma20": "score_rs",
               "inst_streak": "score_chip"}


def _reject_factor_scores(factors):
    """把 radar 的 score_factors（{factor:{points,max,status}}）攤成 watch_reject
    欄位：對得上的填具名欄，完整 points 另存 factors_json 供 Phase 5 分析。"""
    if not isinstance(factors, dict):
        return {}
    out, pts = {}, {}
    for k, v in factors.items():
        p = v.get("points") if isinstance(v, dict) else None
        pts[k] = p
        col = _FACTOR_COL.get(k)
        if col and p is not None:
            out[col] = p
    out["factors_json"] = json.dumps(pts, ensure_ascii=False)
    return out


def judge_watchlist_row(source, close_change, relative,
                        group=None, high=None, entry_ref=None):
    """T+1 收盤依 source 分流判定。純函式，門檻取自 config。
    回傳 (verdict, is_hit, ret) —— is_hit 為「真實命中」（headline 命中率用）。
    close_change: 今日(T+1)收盤漲跌%；因 change_rate 基準＝前一交易日收盤
                  ＝選股日收盤(entry_ref)，故它直接就是相對進場的報酬(%)。
    relative: 個股漲幅 − 族群中位漲幅（相對族群強度，pp）。
    group/high/entry_ref: radar「觀察」來源且有 T+1 最高價時改判四狀態
      （未突破／突破失敗／突破站穩／突破延續）——收盤會吃掉早盤突破，
      單看收盤把「有拉過但被殺」誤判失敗不合理；缺 high 時退回 A/B/C。
    source is None → 回 (None, ...)，呼叫端走舊相容判定。"""
    if close_change is None:
        return ("待資料", False, None)
    ret = close_change / 100.0
    rel_ok = (relative is not None and relative > 0)
    if source == "radar":
        succ = getattr(C, "RADAR_T1_SUCCESS", 0.02)
        cont = getattr(C, "RADAR_T1_CONTINUE_MIN", 0.005)
        if group == "觀察" and high is not None and entry_ref:
            try:
                high_break = float(high) > float(entry_ref)
            except (TypeError, ValueError):
                high_break = False
            # 盤中突破與收盤確認必須分開判斷；只突破盤中高點不等於命中。
            # close_change 的基準是 entry_ref，因此 ret >= 0 等價於
            # today_close >= entry_ref。先擋住「盤中突破、收盤失守」再判續強。
            close_confirmed = ret >= 0
            if high_break and not close_confirmed:
                return ("突破失敗", False, ret)      # 盤中拉過但收盤低於門檻
            if rel_ok and ret >= succ:
                return ("突破延續", True, ret)      # 收盤站穩且續強＝真實命中
            if rel_ok and ret >= 0:
                return ("突破站穩", False, ret)      # 收盤守住進場基準
            if high_break:
                return ("突破失敗", False, ret)      # 盤中拉過但收盤被殺回
            return ("未突破", False, ret)            # 根本沒人拉
        if rel_ok and ret >= succ:
            return ("A_突破成功", True, ret)      # 真正有肉
        if rel_ok and ret >= cont:
            return ("B_續強", False, ret)          # 方向對、未達肉；含續強才算
        return ("C_未續強", False, ret)
    if source == "resilient":
        floor = getattr(C, "RESILIENT_T1_FLOOR", -0.02)
        if rel_ok and (close_change > floor * 100 or close_change > 0):
            return ("抗跌成立", True, ret)
        return ("抗跌失敗", False, ret)
    return (None, False, ret)                       # 舊格式 → 相容判定


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
def _tag_signal_types(wl):
    """選股當下(盤後 T 日)：逐檔用日K判「昨日訊號型態」+ 明日進場觸發價,
    就地寫回 wl 的 signal_type / trigger_price / signal_kind,供 save_watchlist 落庫。
    任何一檔取數/判定失敗都不影響其他檔,也不擋名單產出(型態留空,B 卡退回 source 舊判定)。"""
    for w in wl:
        try:
            bars = broker.daily_kbars(str(w["code"]), days=70)
            r = signal_pattern.classify(bars)
            if r.get("signal_type"):
                w["signal_type"] = r["signal_type"]
                w["signal_kind"] = r["kind"]          # breakout / pullback(給 T+1 觸發判定用)
                if r.get("trigger_price") is not None:
                    w["trigger_price"] = r["trigger_price"]
            else:
                # 無具名型態:仍給預設觸發價,讓 T+1 能算出明確原因(不留『缺觸發價』)
                kind = signal_pattern.kind_of(None, w.get("source"))
                tp = signal_pattern.default_trigger(bars, kind)
                if tp is not None:
                    w["trigger_price"] = tp
        except Exception as exc:
            print(f"[after_hours] 型態判定失敗 {w.get('code')}：{exc}")


def verify_today(snaps, sectors, today_signals_codes, strong_codes):
    """T+1 收盤驗證今日觀察名單（名單於前一交易日晚間產出，故 change_rate
    基準＝選股日收盤，直接就是相對進場的報酬）。依 source 分流判定：
      · radar     → A/B/C（真實命中＝A；含續強＝A+B）
      · resilient → 相對族群為正 且 跌幅未破 -2%
      · 舊格式(source=None) → 相容：有訊號或盤中強勢即命中
    另彙總報酬分布(avg/median/max/min)寫入 review_log，供績效歸因。"""
    tdate = db.today()
    wl = db.load_watchlist(tdate)
    snap_by = {str(s.get("code")): s for s in (snaps or [])}
    sec_pct = {x.get("name"): x.get("pct") for x in (sectors or [])}
    # 盤後 18:00 時 last_state._snaps 常為空 → 收盤價從 radar 快照補
    # （intraday_live_snapshot.json，Phase 4 已證實可靠、含 change_rate）。
    # 缺哪補哪：既有 snaps 優先，未覆蓋的股用快照收盤補上；族群中位同理。
    radar_rows = _radar_snapshot_rows(tdate)
    if radar_rows:
        for r in radar_rows:
            code = str(r.get("code"))
            if code not in snap_by:
                snap_by[code] = {
                    "code": code, "change_rate": r.get("change_rate"),
                    "price": r.get("price"), "high": r.get("high"),
                    "volume_ratio": r.get("volume_ratio"),
                    "group": r.get("group"), "aflow": r.get("aflow")}
        if not sec_pct:
            sec_pct = _sector_medians(radar_rows)

    hit = 0
    returns = []
    outcomes = []
    for w in wl:
        code = w["stock_id"]
        s = snap_by.get(str(code)) or {}
        cc = s.get("change_rate")
        sec_med = sec_pct.get(w.get("sector"))
        rel = (cc - sec_med) if (cc is not None and sec_med is not None) else None
        source = w.get("source")

        verdict, is_hit, ret = judge_watchlist_row(
            source, cc, rel, group=w.get("group_at_pick"),
            high=s.get("high"), entry_ref=w.get("entry_ref"))
        if verdict is None:      # 舊格式相容判定（Monday 首日驗週五舊名單用）
            is_hit = code in today_signals_codes or code in strong_codes
            verdict = "相容命中" if is_hit else "相容未命中"
        if is_hit:
            db.mark_watch_hit(tdate, code)
            hit += 1
        if ret is not None:
            returns.append(ret * 100)     # 存成百分比

        # ── 今日觸發判定 + 未觸發的【明確原因】(取代前端「原定進場條件未成立」) ──
        stype = w.get("signal_type")
        tprice = w.get("trigger_price")
        kind = signal_pattern.kind_of(stype, source)
        trig_status, non_trig_reason = signal_pattern.describe_trigger(
            kind, tprice,
            today_high=s.get("high"), today_low=s.get("low"),
            today_close=s.get("price"), chg=cc,
            volume_ratio=s.get("volume_ratio"), aflow=s.get("aflow"), rel=rel)
        try:
            entry_ref_num = float(w.get("entry_ref"))
            high_num = float(s.get("high"))
            close_num = float(s.get("price"))
            intraday_breakout = high_num >= entry_ref_num
            close_confirmed = close_num >= entry_ref_num
        except (TypeError, ValueError):
            intraday_breakout = None
            close_confirmed = None

        outcomes.append({
            "code": code, "name": w.get("stock_name") or code,
            "sector": w.get("sector"), "watch_reason": w.get("reason"),
            "open_group": w.get("group_at_pick"),
            "close_group": s.get("group"),
            "close_price": s.get("price"), "change_rate": cc,
            "aflow": s.get("aflow"), "volume_ratio": s.get("volume_ratio"),
            "verdict": verdict,
            "entry_ref": w.get("entry_ref"),
            "today_high": s.get("high"),
            "intraday_breakout": intraday_breakout,
            "close_confirmed": close_confirmed,
            "note": f"source={source or '舊格式'} 相對族群{'' if rel is None else f'{rel:+.1f}pp'}",
            "signal_type": stype, "trigger_price": tprice,
            "trigger_status": trig_status, "non_trigger_reason": non_trig_reason,
        })

    missed = sorted(strong_codes - {w["stock_id"] for w in wl})
    stats = {}
    if returns:
        stats = {
            "avg_return": round(statistics.mean(returns), 2),
            "median_return": round(statistics.median(returns), 2),
            "max_return": round(max(returns), 2),
            "min_return": round(min(returns), 2),
        }
    rate = db.write_review(tdate, len(wl), hit, missed,
                           notes="T+1 分流驗證（headline=真實命中A）", stats=stats)
    if outcomes:
        db.save_watch_outcome(tdate, outcomes)
    return {"date": tdate, "total": len(wl), "hit": hit,
            "rate": rate, "missed": missed, "stats": stats}


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
    rejects = []   # 落選池：進了候選圈（資金流出族群）但卡在某因子的檔
    for s in snaps:
        sec = s.get("sector")
        if sec not in out_sectors:
            continue   # 非候選圈，不算落選，不留痕
        sec_pct = next(x["pct"] for x in sectors if x["name"] == sec)

        def _reject(fail, detail):
            rejects.append({
                "code": s["code"], "name": C.NAME_MAP.get(s["code"], s["code"]),
                "sector": sec, "source": "resilient",
                "fail_factor": fail, "detail": detail})

        if s["change_rate"] < sec_pct + 1.5:
            _reject("逆勢<族群+1.5pp",
                    f"個股{s['change_rate']:+.1f}% vs 族群{sec_pct:+.1f}%")
            continue
        if (s["volume_ratio"] or 0) < 0.8:
            _reject("量比<0.8", f"量比{s['volume_ratio'] or 0:.1f}")
            continue
        ch = chips.get_chips(s["code"])
        if ch["inst_net_20d_lots"] is not None and ch["inst_net_20d_lots"] <= 0:
            _reject("法人買超<=0", f"近月{ch['inst_net_20d_lots']:,}張")
            continue
        rows.append({
            "code": s["code"],
            "name": C.NAME_MAP.get(s["code"], s["code"]),
            "sector": sec,
            "source": "resilient",
            "entry_ref": s.get("change_rate"),
            "reason": f"資金流出族群抗跌 逆勢{s['change_rate']:+.1f}% 量比{s['volume_ratio']:.1f}"
                      + (f" 法人買超{ch['inst_net_20d_lots']:,}張"
                         if ch["inst_net_20d_lots"] else ""),
        })
    return rows[:10], rejects   # 上限10檔 + 全部落選留痕


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

    # ① 收盤驗證（T+1 分流；失敗回退舊二元判定，不可讓排程 throw）
    sig_codes = {x["code"] for x in last_state.get("stocks", [])
                 if x["action"] in ("buy", "watch")}
    strong = {s["code"] for s in snaps
              if s.get("change_rate", 0) > 2 and (s.get("volume_ratio") or 0) > 1.5}
    try:
        review = verify_today(snaps, sectors, sig_codes, strong)
    except Exception as exc:
        print(f"[after_hours] T+1 驗證失敗，回退舊判定：{exc}")
        tdate = db.today()
        wl0 = db.load_watchlist(tdate)
        hit0 = sum(1 for w in wl0
                   if w["stock_id"] in sig_codes or w["stock_id"] in strong)
        for w in wl0:
            if w["stock_id"] in sig_codes or w["stock_id"] in strong:
                db.mark_watch_hit(tdate, w["stock_id"])
        missed0 = sorted(strong - {w["stock_id"] for w in wl0})
        review = {"date": tdate, "total": len(wl0), "hit": hit0,
                  "rate": db.write_review(tdate, len(wl0), hit0, missed0,
                                          notes="回退舊判定"),
                  "missed": missed0, "stats": {}}

    # ⓪ ABAB 四象限輪動分析(使用者觀察定案)
    rotation_reports, resilient = rotation_analysis(sectors, snaps)

    # ② 明日觀察清單 = Radar 優先、Resilient 補足（不 union）
    #    抗跌候選 = 原 build_tomorrow_watchlist + 輪動分析抗跌股（去重）
    tomorrow = next_trade_date()
    resilient_rows, resilient_rejects = build_tomorrow_watchlist(sectors, snaps)
    seen = {w["code"] for w in resilient}
    resilient_pool = resilient + [w for w in resilient_rows if w["code"] not in seen]
    for w in resilient_pool:      # 輪動抗跌股補齊 source 標記
        w.setdefault("source", "resilient")
    try:
        radar_rows = _radar_snapshot_rows(db.today())
        wl, radar_rejects = select_radar_watchlist(radar_rows, resilient_pool, limit=10)
        if not wl:                # radar 空又無抗跌 → 保底用抗跌池
            wl, radar_rejects = resilient_pool[:10], []
        src_note = f"radar={sum(1 for w in wl if w.get('source')=='radar')}／resilient={sum(1 for w in wl if w.get('source')=='resilient')}"
    except Exception as exc:
        print(f"[after_hours] radar 名單合流失敗，回退純抗跌：{exc}")
        wl, radar_rejects = resilient_pool[:10], []
        src_note = "回退純抗跌"
    print(f"[after_hours] 明日名單 {tomorrow}：{len(wl)} 檔（{src_note}）")
    _tag_signal_types(wl)          # 選股當下算「昨日訊號型態」+ 明日觸發價,存進 watchlist
    db.save_watchlist(tomorrow, wl)
    # 落選留痕：radar 落選 + 抗跌未過濾 + 抗跌過關但名額被 radar 佔走
    picked = {w["code"] for w in wl}
    slot_lost = [{"code": w["code"], "name": w.get("name"), "sector": w.get("sector"),
                  "source": "resilient", "fail_factor": "名額不足(radar優先)",
                  "detail": w.get("reason")}
                 for w in resilient_pool if w["code"] not in picked]
    # 只留「真淘汰」：具名卡關因子(resilient) + 名額不足；不再落地 radar 整池排除雜訊(每天≈41 檔千篇一律)
    all_rejects = resilient_rejects + slot_lost
    db.save_watch_rejects(tomorrow, [r for r in all_rejects if r["code"] not in picked])

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
