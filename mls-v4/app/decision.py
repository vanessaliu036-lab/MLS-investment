"""
MLS v4.0 — decision.py
核心判斷：主動買賣差 → 象限 → 五模組健康分 → 分級 → 相對強弱。
公式對齊 decision_v22.py 實作，每個健康分可逐項驗算。
"""
import config as C
import absorption
import scoring


# ── 主動買賣差 aflow_ratio ──
def aflow_ratio(af, total_volume, vr=None, chg=None):
    """主動買賣差/總量；無盤中累積→量比近似(vr_proxy)。誠實標記來源。"""
    ratio = 0.0
    if total_volume and total_volume > 0 and af:
        ratio = af / total_volume
    src = "shioaji_aflow"
    if abs(ratio) < 1e-9:
        vr = vr or 0
        chg = chg or 0
        ratio = (0.05 if (vr >= 1.0 and chg >= 0) else
                 -0.05 if (vr >= 1.0 and chg < 0) else
                 0.01 if chg >= 0 else -0.01)
        src = "vr_proxy"
    return round(ratio, 3), src


# ── 象限 ──
def quadrant(ratio, chg):
    flow_in = ratio >= 0
    if flow_in and chg >= 0:
        return "in_up"
    if flow_in:
        return "in_down"
    return "out_up" if chg >= 0 else "out_down"


QUAD_NAME = {"in_up": "真攻擊", "in_down": "假紅", "out_up": "惜售", "out_down": "休息"}
C_QUAD_LABEL = {"in_up": "流入↗漲", "in_down": "流入↗跌",
                 "out_up": "流出↘漲", "out_down": "流出↘跌"}


# ── 籌碼一致性 chip_ok ──
def chip_ok_of(chip):
    """回傳 (chip_ok 1/0/None, note)。對齊 decision_v22._get_chip。"""
    net = chip.get("inst_net_20d_lots")
    streak = chip.get("inst_streak")
    trend = chip.get("big_holder_trend")
    if net is None and streak is None and trend is None:
        return None, "無籌碼資料"
    pos = ((net or 0) > 0) or ((streak or 0) >= 3) or ((trend or 0) > 0)
    neg = ((net or 0) < 0 and (streak or 0) <= -3)
    note = (f"法人近月{(net or 0):+,}張"
            + (f",連{'買' if (streak or 0) > 0 else '賣'}{abs(streak)}日" if streak else "")
            + (f",大戶{trend:+.1f}pp" if trend is not None else ""))
    if pos and not neg:
        return 1, note
    if neg:
        return 0, note
    return None, note


# ── 健康分（五模組加權，可逐項驗算）──
def score_stock(quad, trend, flow_streak, chg, chip_ok, liv=None):
    base = {"in_up": 60, "in_down": 45, "out_up": 40, "out_down": 25}[quad]
    s = base
    s += min(12, max(0, (flow_streak - 1)) * 4)
    s += {"改善": 10, "持平": 0, "惡化": -10, "首日": 0, "新增": 0}.get(trend, 0)
    if chip_ok == 1:
        s += 15
    elif chip_ok == 0:
        s -= 10
    if quad == "in_up" and (chg or 0) >= 2:
        s += 8
    if (chg or 0) <= -3:
        s -= 8
    if liv:
        if liv.get("state") == "上升趨勢":
            s += 5
        if str(liv.get("pivot", "")).startswith("多方"):
            s += 5
    return int(max(0, min(100, s)))


def score_formula(quad, trend, flow_streak, chg, chip_ok):
    """回傳健康分公式拆解字串（給前端顯示）。"""
    base = {"in_up": 60, "in_down": 45, "out_up": 40, "out_down": 25}[quad]
    fs = min(12, max(0, (flow_streak - 1)) * 4)
    tr = {"改善": 10, "持平": 0, "惡化": -10, "首日": 0, "新增": 0}.get(trend, 0)
    cp = 15 if chip_ok == 1 else (-10 if chip_ok == 0 else 0)
    pp = 8 if (quad == "in_up" and (chg or 0) >= 2) else 0
    if (chg or 0) <= -3:
        pp -= 8
    total = max(0, min(100, base + fs + tr + cp + pp))
    return (f"象限基礎({quad}) {base} + 連續{flow_streak}日 +{fs} + "
            f"趨勢({trend}) {tr:+d} + 籌碼 {cp:+d} + 價格 {pp:+d} = {total}")


# ── 分級 ──
def grade_of(quad, trend, score, chip_ok, track="attack", above_ma20=None):
    if track == "engine":
        if above_ma20 and score >= C.ENGINE_READY_MIN and chip_ok != 0:
            return "Ready"
        if above_ma20 or score >= C.WATCH_MIN:
            return "Watch"
        return "Hold"
    if score >= C.READY_MIN and quad == "in_up" and chip_ok != 0:
        return "Ready"
    if score >= C.WATCH_MIN or (quad == "in_down" and trend == "改善"):
        return "Watch"
    return "Hold"


# ── 相對強弱 ──
def relative(chg, sector_chg, market_chg):
    return {
        "stock_chg": chg, "sector_chg": sector_chg, "market_chg": market_chg,
        "vs_sector": round(chg - sector_chg, 2),
        "vs_market": round(chg - market_chg, 2),
        "rs": "領先" if chg > sector_chg else "落後",
    }


# ── 風險檢查 ──
def risk_check(snap, ratio_src, above_ma20, ma20, close, trigger, chip_ok):
    bias = abs((close - ma20) / ma20 * 100) if ma20 else 0
    return {
        "ma_break": not above_ma20,
        "divergence": ratio_src == "vr_proxy" and snap.get("change_rate", 0) > 0
                      and snap.get("aflow", 0) < 0,
        "proxy": ratio_src == "vr_proxy",
        "over_bias": bias > 8,
        "near_limit": snap.get("change_rate", 0) >= 6,
        "no_breakout": close < trigger,
        "resistance": False,
        "data_incomplete": chip_ok is None,
    }


def effective_grade(grade, risk):
    """硬風險封頂：Ready 有硬風險 → Watch。"""
    hard = [k for k in ("ma_break", "divergence", "proxy") if risk.get(k)]
    if grade == "Ready" and hard:
        return "Watch", True, hard
    return grade, False, hard


# ── 交易計畫 ──
def trade_plan(track, close, ma20, trigger):
    atr_stop = round(close * 0.97, 1)
    return {
        "entry": trigger,
        "stop": ma20 if track == "engine" else atr_stop,
        "target": round(close * 1.08, 0),
        "volume": "需>昨",
    }


# ── 綜合評估單一個股（盤後）──
def evaluate_stock(snap, chip, sector_chg, market_chg, ma20,
                   prev_quad=None, prev_streak=0, liv=None):
    """整合五模組，回傳完整評估 dict。"""
    chg = snap["change_rate"]
    ratio, ratio_src = aflow_ratio(snap.get("aflow"), snap.get("volume"),
                                   snap.get("volume_ratio"), chg)
    quad = quadrant(ratio, chg)

    # 連續天數 / 趨勢（對比昨日）
    QUAD_RANK = {"out_down": 0, "in_down": 1, "out_up": 2, "in_up": 3}
    if prev_quad is None:
        streak = 1 if quad.startswith("in") else 0
        trend = "首日"
    else:
        if quad.startswith("in") and str(prev_quad).startswith("in"):
            streak = (prev_streak or 0) + 1
        else:
            streak = 1 if quad.startswith("in") else 0
        d = QUAD_RANK[quad] - QUAD_RANK.get(prev_quad, 1)
        trend = "改善" if d > 0 else ("惡化" if d < 0 else "持平")

    chip_ok, chip_note = chip_ok_of(chip)
    close = snap["close"]
    above_ma20 = close >= ma20 if ma20 else False
    trigger = snap["high"]

    stars, abs_detail = absorption.evaluate(snap, chip)
    rel = relative(chg, sector_chg, market_chg)
    risk = risk_check(snap, ratio_src, above_ma20, ma20, close, trigger, chip_ok)
    def state(value, good, bad):
        if value is None:
            return scoring.NO_DATA
        return scoring.PASS if good(value) else scoring.FAIL if bad(value) else scoring.NO_DATA

    margin_5d = chip.get("margin_5d_chg")
    scoring_input = scoring.StockInput(
        code=snap["code"], name=snap["name"], sector=snap["sector"],
        quadrant=C_QUAD_LABEL.get(quad, quad), day_change_pct=chg,
        active_buysell_diff=ratio, vol_ratio=snap.get("volume_ratio") or 0,
        legal_20d_net=chip.get("inst_net_20d_lots") or 0,
        foreign_20d=chip.get("foreign_lots") or 0, trust_20d=chip.get("invest_lots") or 0,
        legal_5d_net=chip.get("inst_5d_net_lots") or 0, foreign_5d=chip.get("foreign_5d_lots", 0) or 0,
        trust_5d=chip.get("trust_5d_lots") or 0, legal_consec_days=chip.get("inst_streak") or 0,
        margin_5d_chg=margin_5d or 0, close=close, ma20=ma20 or 0,
        above_ma20=above_ma20, bias_pct=((close - ma20) / ma20 * 100) if ma20 else 0,
        foreign_turn_buy=state(chip.get("foreign_lots"), lambda v: v > 500, lambda v: v < -500),
        margin_down=state(margin_5d, lambda v: v < 0, lambda v: v > 0),
        dahu_hold=state(chip.get("big_holder_trend"), lambda v: v >= -0.2, lambda v: v < -0.2),
        price_hold=state(chg, lambda v: v > -2, lambda v: v <= -2),
        vs_sector_pct=rel["vs_sector"], near_limit_up=chg >= 9,
        volume_blowout=(snap.get("volume_ratio") or 0) > 2,
        no_breakout=close < trigger, dahu_custody=scoring.NO_DATA,
    )
    scoring_result = scoring.compute_health_score(scoring_input)
    score = scoring_result["score"]
    eff_grade = scoring_result["grade"]
    capped = bool(scoring_result["hard_risk"])
    hard = list(dict.fromkeys(scoring_result["hard_risk"] +
                              (["ma_break"] if risk.get("ma_break") else []) +
                              (["divergence"] if risk.get("divergence") else [])))
    risk["scoring_hard_risk"] = scoring_result["hard_risk"]
    risk["scoring_soft_risk"] = scoring_result["soft_risk"]
    plan = trade_plan(snap["track"], close, ma20, trigger)

    return {
        "code": snap["code"], "name": snap["name"], "sector": snap["sector"],
        "track": snap["track"], "quad": quad, "score": score,
        "grade": eff_grade, "grade_capped": capped,
        "stars": scoring_result["stars"], "scoring": scoring_result,
        "chg": chg, "ratio": ratio, "ratio_src": ratio_src,
        "vr": snap.get("volume_ratio"), "streak": streak, "trend": trend,
        "chip_ok": chip_ok, "chip_note": chip_note,
        "inst_net_20d": chip.get("inst_net_20d_lots"),
        "inst_streak": chip.get("inst_streak"),
        "big_holder_trend": chip.get("big_holder_trend"),
        "foreign_lots": chip.get("foreign_lots"),
        "invest_lots": chip.get("invest_lots"),
        "dealer_lots": chip.get("dealer_lots", 0),
        "inst_5d_net": chip.get("inst_5d_net_lots"),
        "trust_5d_net": chip.get("trust_5d_lots"),
        "margin_5d_chg": chip.get("margin_5d_chg", 0),
        "close": close, "prev_close": snap["prev_close"],
        "high": snap["high"], "low": snap["low"], "ma20": ma20,
        "near_limit": chg >= 9,
        "above_ma20": above_ma20, "trigger": trigger,
        "sector_chg": sector_chg, "market_chg": market_chg,
        "vs_sector": rel["vs_sector"], "vs_market": rel["vs_market"], "rs": rel["rs"],
        "formula": score_formula(quad, trend, streak, chg, chip_ok),
        "absorption": abs_detail, "risk": risk, "hard_risk": hard,
        "trade_plan": plan,
    }
