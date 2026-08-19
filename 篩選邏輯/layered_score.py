"""
layered_score.py — 盤後「雙分數 × 分層制」評分(取代單分數淘汰制)

為什麼要換掉單分數:
  舊 screen_post 把「今天強不強」「明天有沒有戲」「現在能不能追」壓成一個分數,
  再用分數低→砍。結果強勢股(乖離高、籌碼未補)被一分打死,隔天卻續漲=誤刪。
  (上銀/聯茂 2026-08-06 被砍、隔日全漲,就是這個病。)

本模組拆成兩條獨立分數 + 四層,不再用分數直接刪:
  · continuation_score  延續機率(明天值不值得看):趨勢/族群/相對強度/量價/資金持續/籌碼同步/催化
  · chase_risk          追價風險(現在能不能追):乖離/連漲/爆量/上影收盤/距支撐/跳空
  分層(依使用者定案的優先序):
      結構失效 >= 2 項        → 淘汰(唯一能真刪的門)
      chase_risk >= 70        → 強勢觀察｜禁止追價(不是淘汰)
      continuation >= 65      → 核心觀察
      其餘                     → 候補觀察

三態鐵律:任何因子資料沒到 = Pending,不加不扣,不列入分母。
  籌碼未補 ≠ 法人沒買。漲停缺盤後法人 = 待補,不是負分。

漲停獨立模型:漲停不因「無盤後籌碼」被打成低分;只分「健康漲停/高風險漲停」,
  兩者都不是淘汰,最差是降級。

與 screen_post 的舊 score_one 並存,不共用、不覆蓋。這支改壞不影響舊管線。
綁真實欄位:daily_bar(open/high/low/close/ma5/ma20/ma60/volume/vol_ma20)、
           inst_flow(foreign_net/trust_net/dealer_net/total_net/consecutive_days)。
"""

from __future__ import annotations

from typing import Optional

# ── 籌碼四態(取代 hasChipSupport 布林;規格 §6)────────────────
CHIP_PENDING = "pending"    # 盤後籌碼未到 → 不加不扣
CHIP_POSITIVE = "positive"  # 確認法人買超 → 加分
CHIP_NEUTRAL = "neutral"
CHIP_NEGATIVE = "negative"  # 確認法人賣超 → 扣分

# ── 唯一五分類（潛力與追價狀態分離）─────────────────────────
TIER_CORE = "🔥 A級啟動"
TIER_REVERSAL = "🔄 反轉候選"
TIER_CANDIDATE = "👀 保留觀察"
TIER_NO_CHASE = "⏳ 強勢但不追"
TIER_REJECTED = "❌ 結構失效"

CONT_CORE_MIN = 65    # 延續機率 >= 此值 → 核心
CHASE_RISK_MAX = 70   # 追價風險 >= 此值 → 禁追
STRUCT_FAIL_MIN = 4   # 四重閘門全部成立才真淘汰
HIGH_BIAS_PCT = 7.0   # 距 5MA >= 此值 = 高乖離警戒 → 類別性禁追(規格明列 7%)

# ── A 級細分(2026-08-19 新增,附加欄位,不動 potential_grade 既有 A/B 語意)──
# 同樣 continuation=78,「昨天普通今天突然突破」跟「已經連漲四天今天仍強」是完全不同
# 的 T+1 性質,前者才是明日核心候選該優先的。potential_grade(A/B)算法完全不變,
# potential_tier 只是把 A 再切細,給排序用,不影響任何既有讀 potential_grade 的地方。
POTENTIAL_A1 = "A1"  # Fresh breakout:Day1 首次突破
POTENTIAL_A2 = "A2"  # Continuation:Day2 主升確認,或未啟動但延續分數本身夠高的穩健強勢股
POTENTIAL_A3 = "A3"  # Extended:Day3 強勢延伸／Day4+ 高乖離,且延續分數仍夠高
POTENTIAL_B = "B"

POTENTIAL_LABEL = {
    POTENTIAL_A1: "🔥 A1 首次突破",
    POTENTIAL_A2: "⚡ A2 延續確認",
    POTENTIAL_A3: "⏳ A3 延伸段(高乖離)",
    POTENTIAL_B: "B 一般",
}
# 明日核心候選優先序:數字越小越優先(A1 > A2 > A3 > B)
POTENTIAL_PRIORITY = {POTENTIAL_A1: 0, POTENTIAL_A2: 1, POTENTIAL_A3: 2, POTENTIAL_B: 3}


def potential_tier(lifecycle: str, cont_score: float) -> str:
    """把 potential_grade 的 A 再切成 A1/A2/A3。與舊 potential_grade 公式等價聯集,
    純附加欄位:A1∪A2∪A3 = 舊公式判「A」的全部情況,不會多判也不會少判。"""
    if lifecycle.startswith("🔥 Day 1"):
        return POTENTIAL_A1
    if lifecycle.startswith("🔥 Day 2"):
        return POTENTIAL_A2
    if lifecycle.startswith("⚡ Day 3") or lifecycle.startswith("⏳ Day 4+"):
        return POTENTIAL_A3 if cont_score >= CONT_CORE_MIN else POTENTIAL_B
    return POTENTIAL_A2 if cont_score >= CONT_CORE_MIN else POTENTIAL_B


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════
# 0. 綁真實欄位:bar + inst 兩列 → 正規化輸入
# ══════════════════════════════════════════════════════════
def build_input(code: str, bar: Optional[dict], inst: Optional[dict],
                *, change_rate: Optional[float] = None,
                previous_change_rate: Optional[float] = None,
                previous_bar: Optional[dict] = None,
                aflow_today: Optional[float] = None,
                aflow_previous: Optional[float] = None,
                prior_changes: Optional[list[float]] = None,
                is_limit_up: Optional[bool] = None,
                inst_3d: Optional[float] = None,
                inst_5d: Optional[float] = None,
                sector_rel: Optional[float] = None,
                market_rel: Optional[float] = None,
                up_days: Optional[float] = None,
                catalyst: Optional[bool] = None) -> dict:
    """把 daily_bar / inst_flow 真實列組成評分輸入。缺的留 None=Pending。

    change_rate(今日漲跌%,對昨收)由管線用前一交易日 daily_bar 收盤算;缺則以
    open→close 近似(漲停/收黑判定會略失真,故盡量帶入)。
    sector_rel/market_rel(相對族群/大盤 %)、up_days(連漲天數)、catalyst 目前盤後
    資料源尚未接入 → 預設 None,標 Pending 不扣分,待資料接上再帶入。
    """
    b = bar or {}
    i = inst or {}
    close = _num(b.get("close"))
    volume = _num(b.get("volume"))
    total_net = _num(i.get("total_net"))
    # 法人占成交量比(張/張):有量有淨額才算,否則 None
    inst_ratio = (total_net / volume) if (total_net is not None and volume) else None
    pb = previous_bar or {}
    return {
        "code": code,
        "open": _num(b.get("open")), "high": _num(b.get("high")),
        "low": _num(b.get("low")), "close": close,
        "ma5": _num(b.get("ma5")), "ma20": _num(b.get("ma20")),
        "ma60": _num(b.get("ma60")),
        "volume": volume, "vol_ma20": _num(b.get("vol_ma20")),
        "foreign_net": _num(i.get("foreign_net")),
        "trust_net": _num(i.get("trust_net")),
        "total_net": total_net,
        "inst_ratio": inst_ratio,
        "inst_streak": _num(i.get("consecutive_days")),
        "inst_3d": _num(inst_3d), "inst_5d": _num(inst_5d),
        "sector_rel": _num(sector_rel), "market_rel": _num(market_rel),
        "up_days": _num(up_days), "catalyst": catalyst,
        "change_rate": _num(change_rate),
        "previous_change_rate": _num(previous_change_rate),
        "previous_close": _num(pb.get("close")),
        "previous_volume": _num(pb.get("volume")),
        "previous_ma5": _num(pb.get("ma5")),
        "previous_ma20": _num(pb.get("ma20")),
        "aflow_today": _num(aflow_today),
        "aflow_previous": _num(aflow_previous),
        "prior_changes": [_num(x) for x in (prior_changes or []) if _num(x) is not None],
        "is_limit_up": is_limit_up,
    }


# ── 幾何量:收盤位置、上影線、乖離、量比 ──────────────────────
def _geometry(f: dict) -> dict:
    o, h, l, c = f["open"], f["high"], f["low"], f["close"]
    rng = (h - l) if (h is not None and l is not None) else None
    close_pos = ((c - l) / rng) if (rng and c is not None) else None   # 1=收最高
    upper_shadow = ((h - max(o, c)) / rng) if (rng and None not in (o, c)) else None
    bias5 = ((c - f["ma5"]) / f["ma5"] * 100) if (f["ma5"] and c is not None) else None
    vol_ratio = (f["volume"] / f["vol_ma20"]) if (f["vol_ma20"] and f["volume"] is not None) else None
    # 漲跌幅優先用對昨收的 change_rate;缺才退回 open→close 近似
    change = f.get("change_rate")
    if change is None:
        change = ((c - o) / o * 100) if (o and c is not None) else None
    return {"close_pos": close_pos, "upper_shadow": upper_shadow,
            "bias5": bias5, "vol_ratio": vol_ratio, "change": change, "rng": rng}


def chip_status(f: dict) -> str:
    """四態:總淨額為主,缺資料 = pending。"""
    t = f["total_net"]
    if t is None:
        return CHIP_PENDING
    if t > 0:
        return CHIP_POSITIVE
    if t < 0:
        return CHIP_NEGATIVE
    return CHIP_NEUTRAL


# ══════════════════════════════════════════════════════════
# 1. 延續機率分數(明天值不值得看)。缺因子→不列入分母(Pending)。
# ══════════════════════════════════════════════════════════
def continuation_score(f: dict, g: dict) -> dict:
    parts: list[tuple[str, float, float]] = []  # (名稱, 得點, 權重)
    reasons: list[str] = []
    missing: list[str] = []

    # 趨勢結構 20:多頭排列 + 站上均線
    c = f["close"]
    if c is not None and f["ma5"] and f["ma20"]:
        t = 0.0
        if c >= f["ma5"]:
            t += 0.35
        if f["ma5"] >= f["ma20"]:
            t += 0.30
        if f["ma60"] and f["ma20"] >= f["ma60"]:
            t += 0.20
        if c >= f["ma20"]:
            t += 0.15
        parts.append(("趨勢結構", 20 * t, 20))
        if t >= 0.7:
            reasons.append("多頭排列站上均線")
    else:
        missing.append("趨勢結構")

    # 族群強度 15 / 相對大盤 15:資料未接 → Pending
    if f["sector_rel"] is not None:
        parts.append(("族群強度", 15 * _clamp((f["sector_rel"] + 1) / 3), 15))
        if f["sector_rel"] >= 0.5:
            reasons.append("族群相對強")
    else:
        missing.append("族群強度")
    if f["market_rel"] is not None:
        parts.append(("相對大盤", 15 * _clamp((f["market_rel"] + 1) / 3), 15))
    else:
        missing.append("相對大盤")

    # 量價結構 15:溫和放量 + 收盤位置高
    if g["vol_ratio"] is not None and g["close_pos"] is not None:
        vr = g["vol_ratio"]
        vp = (1.0 if 1.2 <= vr <= 2.5 else 0.5 if 1.0 <= vr < 1.2 else 0.2 if vr > 2.5 else 0.3)
        vp = vp * (0.5 + 0.5 * g["close_pos"])
        parts.append(("量價結構", 15 * vp, 15))
        if vr >= 1.2 and g["close_pos"] >= 0.6:
            reasons.append("價漲量增收盤穩")
    else:
        missing.append("量價結構")

    # 資金持續性 15:法人連買天數 + 占量比(真淨額) + 近3/5日累計為正(連續流入)
    has_any = any(f[k] is not None for k in ("inst_streak", "inst_ratio", "inst_3d", "inst_5d"))
    if has_any:
        fp = 0.0
        if f["inst_streak"] is not None and f["inst_streak"] > 0:
            fp += 0.35 * _clamp(f["inst_streak"] / 5)
        if f["inst_ratio"] is not None and f["inst_ratio"] > 0:
            fp += 0.35 * _clamp(f["inst_ratio"] / 0.06)
        if f["inst_3d"] is not None and f["inst_3d"] > 0:
            fp += 0.15
        if f["inst_5d"] is not None and f["inst_5d"] > 0:
            fp += 0.15
        parts.append(("資金持續性", 15 * _clamp(fp), 15))
        if f["inst_streak"] and f["inst_streak"] >= 2:
            reasons.append(f"法人連買{int(f['inst_streak'])}日")
        if f["inst_3d"] and f["inst_3d"] > 0 and f["inst_5d"] and f["inst_5d"] > 0:
            reasons.append("近3/5日法人連續流入")
    else:
        missing.append("資金持續性")

    # 籌碼同步 10:外資+投信同買
    if f["foreign_net"] is not None and f["trust_net"] is not None:
        both = (1.0 if f["foreign_net"] > 0 and f["trust_net"] > 0
                else 0.5 if f["foreign_net"] > 0 or f["trust_net"] > 0 else 0.0)
        parts.append(("籌碼同步", 10 * both, 10))
    else:
        missing.append("籌碼同步")

    # 催化/題材 10:未接 → Pending
    if f["catalyst"] is not None:
        parts.append(("催化題材", 10 * (1.0 if f["catalyst"] else 0.0), 10))
    else:
        missing.append("催化題材")

    got = sum(p for _, p, _ in parts)
    wsum = sum(w for _, _, w in parts)
    score = round(got / wsum * 100, 1) if wsum else 0.0
    return {"score": score, "reasons": reasons, "missing": missing,
            "coverage": round(wsum, 0)}


# ══════════════════════════════════════════════════════════
# 2. 追價風險分數(現在能不能追;高=越危險)。缺因子→不列入分母。
# ══════════════════════════════════════════════════════════
def chase_risk_score(f: dict, g: dict) -> dict:
    parts: list[tuple[str, float, float]] = []
    risks: list[str] = []
    missing: list[str] = []

    # 乖離率 25:距 5MA 越遠越危險,>=7% 拉滿
    if g["bias5"] is not None:
        r = _clamp(g["bias5"] / 7) if g["bias5"] > 0 else 0.0
        parts.append(("乖離率", 25 * r, 25))
        if g["bias5"] >= 7:
            risks.append(f"乖離 5MA {g['bias5']:.1f}% 過大")
    else:
        missing.append("乖離率")

    # 連漲天數 20:未接(需多日收盤)→ Pending
    if f["up_days"] is not None:
        parts.append(("連漲天數", 20 * _clamp(f["up_days"] / 4), 20))
        if f["up_days"] >= 3:
            risks.append(f"連漲{int(f['up_days'])}日")
    else:
        missing.append("連漲天數")

    # 爆量程度 20:>3x 拉滿
    if g["vol_ratio"] is not None:
        vr = g["vol_ratio"]
        r = _clamp((vr - 1.5) / 1.5) if vr > 1.5 else 0.0
        parts.append(("爆量程度", 20 * r, 20))
        if vr >= 3:
            risks.append(f"爆量 {vr:.1f}x")
    else:
        missing.append("爆量程度")

    # 上影/收盤位置 15:長上影 + 收盤位置低 = 追了容易套
    if g["upper_shadow"] is not None and g["close_pos"] is not None:
        r = _clamp(g["upper_shadow"] / 0.4) * 0.5 + (1 - g["close_pos"]) * 0.5
        parts.append(("上影收盤", 15 * _clamp(r), 15))
        if g["upper_shadow"] >= 0.3:
            risks.append("長上影收弱")
    else:
        missing.append("上影收盤")

    # 距支撐距離 10:距 5MA 越遠、跌破時越痛
    if g["bias5"] is not None:
        parts.append(("距支撐", 10 * (_clamp(g["bias5"] / 8) if g["bias5"] > 0 else 0.0), 10))
    else:
        missing.append("距支撐")

    # 隔日跳空風險 10:未接(需 T+1)→ Pending
    if f.get("gap_risk") is not None:
        parts.append(("跳空風險", 10 * _clamp(f["gap_risk"]), 10))
    else:
        missing.append("跳空風險")

    got = sum(p for _, p, _ in parts)
    wsum = sum(w for _, _, w in parts)
    score = round(got / wsum * 100, 1) if wsum else 0.0
    return {"score": score, "risks": risks, "missing": missing,
            "coverage": round(wsum, 0)}


# ══════════════════════════════════════════════════════════
# 3. 結構失效（四道閘門全中才真淘汰；Pending 不算成立）
# ══════════════════════════════════════════════════════════
# 四類結構失效（V3 2026-08-13）：淘汰需四類同時成立。
# 治「單根黑 K 被算成兩項結構失效」的重複計分(跌破月線 vs 法人賣超且收黑,兩者都含價格弱勢)。
_FAIL_CATEGORY = {
    "跌破月線": "價格結構",
    "爆量長上影收黑": "量價結構",
    "法人連續賣超": "法人籌碼",
    "族群明顯落後": "族群結構",
}


def failure_categories(fails: list[str]) -> set[str]:
    """把失效項映射到四大類,回傳「不同類」集合。淘汰門檻數的是類別數,不是項數。"""
    return {_FAIL_CATEGORY.get(x, x) for x in fails}


def failure_gates(f: dict, g: dict) -> dict[str, bool]:
    """唯一淘汰門：四項必須全部為真；缺資料自然為 False。"""
    c, h = f.get("close"), f.get("high")
    ma5, ma20 = f.get("ma5"), f.get("ma20")
    price_broken = (None not in (c, ma5, ma20) and c < ma5 and c < ma20)

    af_t, af_y = f.get("aflow_today"), f.get("aflow_previous")
    persistent_outflow = (af_t is not None and af_y is not None and af_t < 0 and af_y < 0)

    vol, prev_vol = f.get("volume"), f.get("previous_volume")
    price_down = g.get("change") is not None and g["change"] < 0
    volume_expanded = (vol is not None and prev_vol is not None and vol > prev_vol)
    blowout_weak = (g.get("vol_ratio") is not None and g["vol_ratio"] >= 1.5 and
                    g.get("close_pos") is not None and g["close_pos"] <= 0.5)
    volume_price_weak = bool(price_down and (volume_expanded or blowout_weak))

    touched_average = (None not in (h, ma5, ma20) and h >= min(ma5, ma20))
    rebound_failed = bool(price_broken and touched_average)
    return {
        "價格結構破壞": bool(price_broken),
        "主動資金持續流出": bool(persistent_outflow),
        "量價轉弱": volume_price_weak,
        "反彈失敗": rebound_failed,
    }


def structural_failures(f: dict, g: dict) -> list[str]:
    """回傳成立的四重閘門，供顯示與複盤；不是任兩項投票制。"""
    return [name for name, hit in failure_gates(f, g).items() if hit]


def reversal_signals(f: dict, g: dict) -> list[str]:
    """抓弱轉強背離；每一項只負責升為反轉候選，不負責淘汰。"""
    signals: list[str] = []
    change = g.get("change")
    c, o, l = f.get("close"), f.get("open"), f.get("low")
    ma5, ma20 = f.get("ma5"), f.get("ma20")
    streak, total_net = f.get("inst_streak"), f.get("total_net")
    inst_selling = ((streak is not None and streak < 0) or
                    (total_net is not None and total_net < 0))
    price_resists = (change is not None and change >= 0) or (None not in (c, o) and c >= o)
    if inst_selling and price_resists:
        signals.append("法人賣超但價格抗跌")
    if (f.get("aflow_previous") is not None and f["aflow_previous"] < 0 and
            f.get("aflow_today") is not None and f["aflow_today"] > 0):
        signals.append("主動資金由負翻正")
    if None not in (l, c, ma5) and l < ma5 <= c:
        signals.append("跌破 MA5 後收復")
    if None not in (l, c, ma20) and l < ma20 <= c:
        signals.append("跌破月線後收復")
    if g.get("vol_ratio") is not None and g["vol_ratio"] >= 1.5 and price_resists:
        signals.append("大量賣壓但股價不跌")
    if (f.get("previous_change_rate") is not None and f["previous_change_rate"] < 0 and
            change is not None and change > 0 and f.get("aflow_today") is not None and
            f["aflow_today"] > 0):
        signals.append("前日弱勢、今日價量背離轉強")
    if (f.get("sector_rel") is not None and f["sector_rel"] < 0 and
            change is not None and change > 0 and f.get("aflow_today") is not None and
            f["aflow_today"] > 0):
        signals.append("族群落後但資金突然集中")
    return list(dict.fromkeys(signals))


def lifecycle_stage(f: dict, g: dict) -> str:
    """以最近交易日的強勢延伸辨識 Day 1～Day 4+。"""
    change = g.get("change")
    prior = f.get("prior_changes") or []
    if change is None or change < 5:
        return "未啟動"
    if change >= 9 and not any(x >= 9 for x in prior[:10]):
        return "🔥 Day 1 首次突破"
    run = 1
    for x in prior:
        if x >= 5:
            run += 1
        else:
            break
    if prior and prior[0] >= 9:
        run = max(run, 2)
    if run == 2:
        return "🔥 Day 2 主升確認"
    if run == 3:
        return "⚡ Day 3 強勢延伸"
    if run >= 4:
        return "⏳ Day 4+ 高乖離／等待整理"
    return "未啟動"


# ══════════════════════════════════════════════════════════
# 4. 漲停獨立模型(不因無籌碼被打低;只分健康/高風險,都非淘汰)
# ══════════════════════════════════════════════════════════
def limit_up_model(f: dict, g: dict) -> Optional[dict]:
    ch = g["change"]
    exact = f.get("is_limit_up")
    is_limit = bool(exact) if exact is not None else (ch is not None and ch >= 9.5)
    if not is_limit:
        return None
    healthy = True
    notes: list[str] = []
    if f["up_days"] is not None and f["up_days"] >= 3:
        healthy = False
        notes.append(f"連漲{int(f['up_days'])}日")
    if g["vol_ratio"] is not None and g["vol_ratio"] > 3:
        healthy = False
        notes.append("爆量>3倍")
    if g["upper_shadow"] is not None and g["upper_shadow"] >= 0.3:
        healthy = False
        notes.append("長上影")
    if f["sector_rel"] is not None and f["sector_rel"] < 0:
        healthy = False
        notes.append("族群未同步")
    return {
        "is_limit_up": True, "healthy": healthy,
        "label": "漲停觀察(健康)" if healthy else "漲停·高風險降級",
        "notes": notes,
    }


# ══════════════════════════════════════════════════════════
# 5. 分層(使用者定案的優先序;分數只排序,不單獨刪)
# ══════════════════════════════════════════════════════════
def classify(cont: float, chase: float, fails: list[str],
             chase_block: bool = False, turns: Optional[list[str]] = None,
             lifecycle: str = "未啟動") -> str:
    if len(fails) == STRUCT_FAIL_MIN:
        return TIER_REJECTED
    strong = lifecycle != "未啟動" or cont >= CONT_CORE_MIN
    if turns and not strong:
        return TIER_REVERSAL
    if (chase_block or chase >= CHASE_RISK_MAX) and strong:
        return TIER_NO_CHASE
    if strong:
        return TIER_CORE
    return TIER_CANDIDATE


def score_layered(f: dict) -> dict:
    """一檔完整評分 → 雙分數 + 四層 + 結構失效 + 漲停模型 + 籌碼四態。"""
    g = _geometry(f)
    cont = continuation_score(f, g)
    chase = chase_risk_score(f, g)
    gates = failure_gates(f, g)
    fails = structural_failures(f, g)
    turns = reversal_signals(f, g)
    lifecycle = lifecycle_stage(f, g)
    limit = limit_up_model(f, g)
    # 高乖離(距5MA≥7%)或漲停 → 類別性禁追閘,不讓收盤穩等好因子把追價風險稀釋掉
    high_bias = g["bias5"] is not None and g["bias5"] >= HIGH_BIAS_PCT
    late_stage = lifecycle.startswith("⚡ Day 3") or lifecycle.startswith("⏳ Day 4+")
    chase_block = high_bias or (limit is not None) or late_stage
    tier = classify(cont["score"], chase["score"], fails,
                    chase_block=chase_block, turns=turns, lifecycle=lifecycle)
    ptier = potential_tier(lifecycle, cont["score"])
    potential = POTENTIAL_B if ptier == POTENTIAL_B else "A"
    entry_status = "禁止追高" if (chase_block or chase["score"] >= CHASE_RISK_MAX) else "等待觸發"
    return {
        "code": f["code"],
        "continuation": cont["score"],
        "chase_risk": chase["score"],
        "chase_safety": round(100 - chase["score"], 1),
        "tier": tier,
        "classification": tier,
        "potential_grade": potential,
        "potential_tier": ptier,
        "potential_label": POTENTIAL_LABEL[ptier],
        "potential_priority": POTENTIAL_PRIORITY[ptier],
        "trend_stage": lifecycle,
        "entry_status": entry_status,
        "chip_status": chip_status(f),
        "failure_gates": gates,
        "failure_gate_count": sum(1 for hit in gates.values() if hit),
        "structural_failures": fails,
        "turn_signals": turns,
        "limit_up": limit,
        "reasons": cont["reasons"],
        "risks": chase["risks"],
        "pending": sorted(set(cont["missing"] + chase["missing"])),
    }


# ══════════════════════════════════════════════════════════
# 自我驗證:截圖四檔(上銀/聯茂 漲停高乖離、富喬 假紅、華星光 漲停)
#   python3 篩選邏輯/layered_score.py
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    # 以合理量價重建四檔(本機無 08-06 真實 DB,故以貼近截圖的數值示範分層邏輯)
    samples = [
        # 上銀:+8.01%、乖離高、爆量、收最高、盤後法人未補(Pending)
        ("2049 上銀", build_input("2049",
            {"open": 246, "high": 259, "low": 245, "close": 259,
             "ma5": 239, "ma20": 228, "ma60": 215, "volume": 42000, "vol_ma20": 12000},
            None, change_rate=8.01, up_days=1)),
        # 聯茂:+4.4%、乖離中(<7%)、放量、收高、法人小買
        ("6213 聯茂", build_input("6213",
            {"open": 108, "high": 113, "low": 107.5, "close": 113,
             "ma5": 106, "ma20": 101, "ma60": 96, "volume": 30000, "vol_ma20": 14000},
            {"foreign_net": 1200, "trust_net": 300, "total_net": 1500, "consecutive_days": 2},
            change_rate=4.4)),
        # 富喬:假紅衝高、拉高收下影、法人賣超、收黑
        ("1815 富喬", build_input("1815",
            {"open": 39, "high": 41.5, "low": 38.6, "close": 38.9,
             "ma5": 39.5, "ma20": 40.2, "ma60": 41, "volume": 60000, "vol_ma20": 20000},
            {"foreign_net": -800, "trust_net": -200, "total_net": -1000, "consecutive_days": -1},
            change_rate=-0.8)),
        # 華星光:+9.8%、光通訊族群同步、收高
        ("4979 華星光", build_input("4979",
            {"open": 188, "high": 199, "low": 187, "close": 199,
             "ma5": 182, "ma20": 170, "ma60": 158, "volume": 25000, "vol_ma20": 11000},
            None, change_rate=9.8, sector_rel=1.8, up_days=2)),
    ]
    print("=== 雙分數分層 scorer · 截圖四檔 ===\n")
    for name, f in samples:
        r = score_layered(f)
        lu = f"  漲停模型:{r['limit_up']['label']}" if r["limit_up"] else ""
        print(f"[{name}]")
        print(f"  延續機率 {r['continuation']}／100   追價風險 {r['chase_risk']}／100(安全 {r['chase_safety']})")
        print(f"  分層:{r['tier']}   籌碼:{r['chip_status']}   結構失效:{r['structural_failures'] or '無'}")
        if lu:
            print(lu)
        print(f"  理由:{r['reasons']}")
        print(f"  風險:{r['risks']}")
        print(f"  待補(Pending,不扣分):{r['pending']}")
        print()
