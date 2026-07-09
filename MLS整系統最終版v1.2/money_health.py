"""
MLS 插件 — money_health.py
資金健康度引擎 + Level 8.1 證據三角交叉驗證(Evidence Triangulation)
====================================================================
純插件:只讀主系統資料,不改任何主邏輯。供 /api/state 增補 health 欄位、
供 nexora 盤後報告呼叫。

核心:資金流向 × 漲跌關係 → 健康度分級(不做「流入=多」單維判斷)
  健康(in_up)   資金流入 + 上漲       → 真攻擊
  假紅(in_down) 資金流入 + 下跌       → 邊拉邊賣/砸盤被算主動買
  惜售(out_up)  資金流出 + 上漲       → 量縮惜售,續航存疑
  休息(out_down)資金流出 + 下跌       → 輪動退潮

Level 8.1:決策前強制三根異質證據(A資金/B價量/C技術/D結構/E外部),
  同源不算;三方矛盾 → 強制「等待確認」並輸出下一驗證訊號。
"""

import config as C
import scoring


HEALTH_LABEL = {
    "in_up":    ("健康", "★★★★★", "資金流入且上漲,價量同向,真攻擊"),
    "in_down":  ("假紅", "★★☆☆☆", "資金流入但下跌,邊拉邊賣/砸盤疑慮,等外資蓋章"),
    "out_up":   ("惜售", "★★★☆☆", "資金流出但上漲,量縮惜售,續航存疑"),
    "out_down": ("休息", "★☆☆☆☆", "資金流出且下跌,輪動退潮,不接刀"),
    "unknown":  ("未連線", "—", "tick 資料未連線(broker 沒回逐筆/total_volume),健康度暫不評,先看其他因子"),
}


def stock_health(s):
    """
    個股資金健康度。回傳 dict:
      quadrant, label, stars, desc, health_score(0-100), aflow_ratio
    若 aflow 是 None(tick 未連線),quadrant=unknown,健康分 = 中性 50,UI 顯示「未連線」
    """
    aflow = scoring.get_aflow(s["code"])
    chg = s.get("change_rate") or 0
    # ── tick 未連線路徑:不要假裝有方向 ──
    if aflow is None:
        return {"quadrant": "unknown", "label": HEALTH_LABEL["unknown"][0],
                "stars": HEALTH_LABEL["unknown"][1], "desc": HEALTH_LABEL["unknown"][2],
                "health_score": 50, "aflow_ratio": None}

    tv = s.get("total_volume") or 1
    ratio = aflow / tv                      # +主動買 / -主動賣
    # ── 量差太小路徑:aflow 是 0 但 sign 累計不出有效方向(可能剛開盤、可能量差太小)
    # 跟 None 差別:None 是「完全沒連線」,這條是「連線了但沒累積到有效訊號」
    # 兩者對決策都是「等資料齊全再說」,給同樣 unknown label
    if abs(aflow) < 100:
        return {"quadrant": "unknown", "label": HEALTH_LABEL["unknown"][0],
                "stars": HEALTH_LABEL["unknown"][1], "desc": "量差太小(<100 股),看不出明顯買賣方向,等盤中量累積再評",
                "health_score": 50, "aflow_ratio": round(ratio, 3)}

    flow_in = ratio >= 0

    if flow_in and chg >= 0:   quad = "in_up"
    elif flow_in and chg < 0:  quad = "in_down"
    elif not flow_in and chg >= 0: quad = "out_up"
    else: quad = "out_down"

    label, stars, desc = HEALTH_LABEL[quad]
    # 健康分:同向加成、背離扣分
    base = 50 + ratio * 50                  # 資金方向
    if quad == "in_up":     base += min(20, chg * 4)
    elif quad == "in_down": base -= min(30, abs(chg) * 5 + abs(ratio) * 20)  # 假紅重扣
    elif quad == "out_up":  base -= 10
    else:                   base -= min(25, abs(chg) * 4)
    score = int(max(0, min(100, base)))
    return {"quadrant": quad, "label": label, "stars": stars, "desc": desc,
            "health_score": score, "aflow_ratio": round(ratio, 3)}


def sector_health(sector_name, members):
    """族群資金健康度:成員健康分中位 + 族群流向×漲跌象限。"""
    if not members:
        return None
    from statistics import median
    scores = [m["_health"]["health_score"] for m in members if m.get("_health")]
    med = median(scores) if scores else 50
    # 族群層象限用成員 aflow 加總 vs 漲幅中位
    aflows = [scoring.get_aflow(m["code"]) for m in members]
    # 全 None → tick 未連線
    if all(a is None for a in aflows):
        return {"quadrant": "unknown", "label": HEALTH_LABEL["unknown"][0],
                "stars": HEALTH_LABEL["unknown"][1],
                "health_score": int(med), "advice": HEALTH_LABEL["unknown"][2]}
    net = sum(a for a in aflows if a is not None)
    chg_med = median([m.get("change_rate") or 0 for m in members])
    flow_in = net >= 0
    if flow_in and chg_med >= 0: quad = "in_up"
    elif flow_in: quad = "in_down"
    elif chg_med >= 0: quad = "out_up"
    else: quad = "out_down"
    label, stars, desc = HEALTH_LABEL[quad]
    return {"quadrant": quad, "label": label, "stars": stars,
            "health_score": int(med), "advice": desc}


# ════════════════════════════════════════════════════════
# Level 8.1 證據三角交叉驗證
# ════════════════════════════════════════════════════════
# 五類證據來源(同類不得重複計為獨立支柱)
def gather_evidence(s, health, sector_pct, market_pct, chip):
    """
    回傳 evidences: [(類別, 方向 +1/-1/0, 說明)]
    類別:A資金 B價量 C技術 D結構 E外部(外部盤中不可得,標中立)
    """
    ev = []
    chg = s.get("change_rate") or 0
    # A 資金面
    r = health["aflow_ratio"]
    ev.append(("A", 1 if r > 0.05 else (-1 if r < -0.05 else 0),
               f"主動淨流 {r:+.2f}"))
    # B 價量結構
    vr = s.get("volume_ratio") or 0
    avgp = s.get("avg_price") or 0
    if avgp and s["price"] >= avgp and vr >= 1.2:
        ev.append(("B", 1, f"站均價線且量比{vr:.1f}"))
    elif avgp and s["price"] < avgp:
        ev.append(("B", -1, f"跌破均價線,量比{vr:.1f}"))
    else:
        ev.append(("B", 0, f"量比{vr:.1f} 價量中性"))
    # C 技術(趨勢)
    if s.get("high") and s["price"] >= s["high"] and chg > 0:
        ev.append(("C", 1, "突破今高"))
    elif chg < -1:
        ev.append(("C", -1, f"技術轉弱 {chg:.1f}%"))
    else:
        ev.append(("C", 0, "技術中性"))
    # D 市場結構(相對強弱)
    rs = chg - sector_pct
    rs_mkt = chg - market_pct
    if rs > 1 and rs_mkt > 0:
        ev.append(("D", 1, f"強於族群{rs:+.1f}pp、強於大盤"))
    elif rs < -1:
        ev.append(("D", -1, f"弱於族群{rs:+.1f}pp"))
    else:
        ev.append(("D", 0, "族群內中庸"))
    # E 外部(盤中不可得)
    ev.append(("E", 0, "外部盤中未取,盤後以國際盤驗證"))
    return ev


def triangulate(evidences):
    """
    三角交叉:至少 3 個異質類別、方向共識判定。
    回傳 dict: verdict(bullish/bearish/pending), strength, log, next_signal
    """
    # 取有明確方向(非0)的異質類別
    directional = [(cat, d, why) for cat, d, why in evidences if d != 0]
    cats = {cat for cat, _, _ in directional}
    pos = [e for e in directional if e[1] > 0]
    neg = [e for e in directional if e[1] < 0]
    neutral = [(c, d, w) for c, d, w in evidences if d == 0]

    log = {"positive": [f"{c}:{w}" for c, d, w in pos],
           "negative": [f"{c}:{w}" for c, d, w in neg],
           "neutral": [f"{c}:{w}" for c, d, w in neutral]}

    # 異質獨立性:少於3個異質「明確方向」類別 → 證據不足,pending
    if len(cats) < 2 or len(directional) < 2:
        return {"verdict": "pending", "strength": "—",
                "conflict": f"僅 {len(cats)} 類異質證據具明確方向,不足三角",
                "log": log, "next_signal": _next_signal()}

    np, nn = len(pos), len(neg)
    if np >= 2 and nn == 0:
        strength = "強" if np >= 3 else "中"
        return {"verdict": "bullish", "strength": strength,
                "conflict": f"{np} 正向 vs 0 反向", "log": log, "next_signal": None}
    if nn >= 2 and np == 0:
        strength = "強" if nn >= 3 else "中"
        return {"verdict": "bearish", "strength": strength,
                "conflict": f"{nn} 反向 vs 0 正向", "log": log, "next_signal": None}
    if np >= 2 and nn >= 1 and np > nn:
        return {"verdict": "bullish", "strength": "弱",
                "conflict": f"{np} 正向 vs {nn} 反向(主流偏多但有雜訊)",
                "log": log, "next_signal": None}
    if nn >= 2 and np >= 1 and nn > np:
        return {"verdict": "bearish", "strength": "弱",
                "conflict": f"{nn} 反向 vs {np} 正向", "log": log, "next_signal": None}
    # 1:1:1 或強度極弱 → 等待確認
    return {"verdict": "pending", "strength": "—",
            "conflict": f"{np} 正向 vs {nn} 反向,方向矛盾,今日無明確方向",
            "log": log, "next_signal": _next_signal()}


def _next_signal():
    return ("明日開盤 30 分鐘內是否站穩今日收盤價 ±0.5% 區間:"
            "突破上緣→突破確認;跌破下緣→賣壓確認。")


# ════════════════════════════════════════════════════════
# Phase A — 決策卡（AI Score / Confidence / State / Action /
#                   Trigger / Invalidation / 進場停損目標）
# 純推導:輸入 health + tri + chip + avg_price,輸出完整決策卡
# 不動主邏輯、不打新 API
# ════════════════════════════════════════════════════════
def build_decision_card(s):
    """
    從一支 snap(已 annotate 過,有 _health / _tri / _chip)推導 6 欄位決策卡。
    回傳 dict: ai_score, confidence, state, action, trigger, invalidation,
               entry, stop, target
    """
    h = s.get("_health") or {}
    t = s.get("_tri") or {}
    c = s.get("_chip") or {}
    price = s.get("price") or 0
    avgp = s.get("avg_price") or 0
    high = s.get("high") or 0
    low = s.get("low") or 0
    vr = s.get("volume_ratio") or 0
    chg = s.get("change_rate") or 0
    quad = h.get("quadrant", "out_down")
    hs = h.get("health_score", 50)
    verdict = t.get("verdict", "pending")
    strength = t.get("strength", "—")
    log = t.get("log") or {}
    pos_n = len(log.get("positive", []))
    neg_n = len(log.get("negative", []))

    # ─── AI Score: 健康分(0-100)→ 排名分(0-100,線性) ───
    ai_score = hs

    # ─── Confidence: 從證據共識度推導 ───
    # ≥3 正向且 0 反向 → 92%
    # ≥2 正向且 0 反向 → 82%
    # 正向 > 反向(弱) → 70%
    # 1:1 / 矛盾 → 50%
    # 反向為主 → 30%
    # 其餘(1 正向 0 反向 等不完整訊號)→ 60%
    if pos_n >= 3 and neg_n == 0:
        conf = 92
    elif pos_n >= 2 and neg_n == 0:
        conf = 82
    elif pos_n > neg_n and pos_n >= 2:
        conf = 70
    elif pos_n == 0 and neg_n == 0:
        conf = 50
    elif pos_n == neg_n:
        conf = 50
    elif neg_n > pos_n:
        conf = 30
    else:
        conf = 60  # 例如 1 正向 0 反向、3 反向 0 正向以外的情形
    # ─── Confidence 修補(2026-07-09):健康度高但 verdict bear/pending 卡死在 30% 的 bug。
    # 當證據全反推時,以 health_score 對 verdict 給**校正基線**,
    # 使「健康度高(>60)的 bearish/pending 卡」至少有 45~55% 而非 30%。
    # 仍保留正負因子鏈最高 92% 的判斷能力(正向情況不修)。
    if conf == 30:
        if hs >= 70 and quad in ("in_up", "in_down"):
            conf = 55  # 高健康度 + 有方向(即使是弱勢)應至少 55
        elif hs >= 60:
            conf = 45  # 健康度尚可,給 45 而非 30
        # 反之 (hs < 50) 保持 30 — 確實資料混亂 / 健康度差
    # 其他正向情況 92/82/70/50/60 全部保留,不壓低
    # verdict 強弱加成
    if verdict == "bullish" and strength == "強":
        conf = min(95, conf + 3)
    elif verdict == "bearish" and strength == "強":
        conf = max(15, conf - 3)

    # ─── State: Ready / Watch / Hold ───
    # Ready = verdict=bullish 且 強 / 中 且 健康分 ≥ 60
    # Watch = verdict=bullish 弱 或 verdict=pending 但共識偏多
    # Hold = verdict=bearish 或 pending 且證據不足 或 健康分 < 50
    # tick 未連線:健康分 = 50 中性 → 一律 Hold(資料不足,不進場)
    if quad == "unknown":
        state = "Hold"
    elif verdict == "bullish" and strength in ("強", "中") and hs >= 60:
        state = "Ready"
    elif verdict == "bullish" and strength == "弱":
        state = "Watch"
    elif verdict == "pending" and pos_n > neg_n and pos_n >= 2:
        state = "Watch"
    elif verdict == "bearish" and strength in ("強", "中"):
        state = "Hold"
    else:
        state = "Hold"

    # ─── Action: 用 rule template ───
    # 大戶連買(>3日)+ verdict=bullish → 「可分批布局」/「等突破即可布局」
    inst_streak = c.get("inst_streak") or 0
    big_pct = c.get("big_holder_pct")
    big_trend = c.get("big_holder_trend")
    if quad == "unknown":
        action = "tick 資料未連線,暫不評估,等資金流上線再判斷"
    elif state == "Ready":
        if inst_streak >= 3:
            action = "可分批布局"
        else:
            action = "等突破昨日高點即可布局"
    elif state == "Watch":
        if quad == "in_down":
            action = "等量縮止跌"
        elif quad == "out_up":
            action = "等籌碼沉澱"
        else:
            action = "等站回均價線"
    else:  # Hold
        if big_trend == "down":
            action = "籌碼仍待改善,暫不介入"
        else:
            action = "暫不介入,等趨勢翻多"

    # ─── Trigger: 進場觸發(條件句) ───
    if quad == "unknown":
        trigger = "tick 資料連線恢復後,依「站回均價線 + 量>1.5x + 法人連買」標準觸發"
    else:
        triggers = []
        if avgp and price < avgp:
            triggers.append(f"站回均價線 {avgp:.1f}")
        if vr < 1.2:
            triggers.append("成交量>5日均量1.5倍")
        if inst_streak and inst_streak < 3:
            triggers.append("法人連買≥3日")
        # 預設觸發(避免空清單)
        if not triggers:
            triggers.append("明日開盤站穩今日高點")
        trigger = " + ".join(triggers[:3])  # 最多 3 條,避免太長

    # ─── Invalidation: 失效條件 ───
    if quad == "unknown":
        invalidation = "資料未連線期間,任何結論都先視為待確認"
    else:
        invalidations = []
        if low and price:
            invalidations.append(f"跌破前低 {low:.1f}")
        if inst_streak and inst_streak >= 2:
            invalidations.append("法人由連買轉連賣")
        # 預設失效
        invalidations.append("資金流轉負且量增下跌")
        invalidation = " / ".join(invalidations[:3])

    # ─── 進場價 / 停損 / 目標(用既有技術指標湊) ───
    # 進場 = 現價 ±1% 區間(實戰以 trigger 觸發後的突破價為主,這裡給參考)
    entry = round(price * 1.005, 2) if price else None
    # 停損 = 今日低點 - 0.5% 或現價 -3%
    if low and price:
        stop = round(min(low * 0.995, price * 0.97), 2)
    else:
        stop = None
    # 目標 = 現價 +5% 或 今日高點 +1%
    if high and price:
        target = round(max(price * 1.05, high * 1.01), 2)
    else:
        target = None

    return {
        "ai_score": ai_score,
        "confidence": conf,
        "state": state,
        "action": action,
        "trigger": trigger,
        "invalidation": invalidation,
        "entry": entry,
        "stop": stop,
        "target": target,
    }


def annotate_with_decision(snaps, sectors, market_pct=0.0):
    """
    annotate + 每檔 snap 增補 _decision 決策卡
    """
    sh_map, counts = annotate(snaps, sectors, market_pct)
    for s in snaps:
        s["_decision"] = build_decision_card(s)
    return sh_map, counts


# ════════════════════════════════════════════════════════
# 主入口:為一批 snapshot 增補 health + triangulation
# ════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════
# v3 — Chip Score(法人買賣超→0-25 分)
# + 時間序列健康分(昨日基準比對)
# + 命中率統計存檔介面
# 純插件延伸:不改主邏輯、不打新 API
# ════════════════════════════════════════════════════════

def chip_score(chip):
    """
    Chip Score(0-25 分):把法人買賣超資料轉成分數。
    分級依使用者規格(2026-07-09):
      主力大買(>=+5000 張近 20 日) → 25
      主力小買(+500~+5000)         → 15
      中性(-500~+500)              → 10
      小賣(-5000~-500)             → 5
      大賣(<=-5000)                → 0
    inst_streak 加成:連買>=3 日 +3、>=5 日 +5(上限)
    inst_streak 扣分:連賣>=3 日 -3、>=5 日 -5(下限)
    最終夾在 0~25 之間。
    """
    if not chip or chip.get("inst_net_20d_lots") is None:
        return {"score": None, "level": "no_data", "reason": "FinMind 無資料(法人買賣超未取得)"}

    lots = chip["inst_net_20d_lots"]
    streak = chip.get("inst_streak") or 0

    # 1) 基準分(看 20 日合計張數)
    if lots >= 5000:
        base = 25; level = "big_buy"; reason = f"法人近20日大買超 +{lots:.0f} 張"
    elif lots >= 500:
        base = 15; level = "small_buy"; reason = f"法人近20日小買超 +{lots:.0f} 張"
    elif lots > -500:
        base = 10; level = "neutral"; reason = f"法人近20日中性 {lots:+.0f} 張"
    elif lots > -5000:
        base = 5; level = "small_sell"; reason = f"法人近20日小賣超 {lots:.0f} 張"
    else:
        base = 0; level = "big_sell"; reason = f"法人近20日大賣超 {lots:.0f} 張"

    # 2) 連買連賣加成
    streak_adj = 0
    if streak >= 5:
        streak_adj = 5
        reason += f",外資連買{streak}日(+5)"
    elif streak >= 3:
        streak_adj = 3
        reason += f",外資連買{streak}日(+3)"
    elif streak <= -5:
        streak_adj = -5
        reason += f",外資連賣{abs(streak)}日(-5)"
    elif streak <= -3:
        streak_adj = -3
        reason += f",外資連賣{abs(streak)}日(-3)"

    score = max(0, min(25, base + streak_adj))
    return {"score": score, "level": level, "reason": reason,
            "base": base, "streak_adj": streak_adj,
            "inst_net_20d_lots": lots, "inst_streak": streak}


def recompute_health_score(s):
    """
    v3 公式:健康分 = 資金流分(0-50) + 價量分(0-20) + 族群分(0-5) + Chip Score(0-25)
    原 stock_health 公式為 base = 50 + ratio*50 + 四象限加成(最多 ±30)
    本函數只重組「健康分」這一維,其他欄位(quadrant/label/stars/desc)沿用 _health。
    資金流分:base 0-50(原公式 base)
    價量分:0-20(看量比 + 站/跌均價)
    族群分:0-5(強於族群 +5、其餘 0)
    Chip Score:0-25(chip_score 結果)
    最終夾在 0-100 之間。
    """
    h = s.get("_health") or {}
    if h.get("quadrant") == "unknown":
        # tick 未連線路徑:不重算(沿用中性 50,避免假訊號)
        return h

    aflow_ratio = h.get("aflow_ratio")
    if aflow_ratio is None:
        return h

    chg = s.get("change_rate") or 0
    vr = s.get("volume_ratio") or 0
    avgp = s.get("avg_price") or 0
    price = s.get("price") or 0
    sec_pct = s.get("_sector_pct") or 0

    # A 資金流分 0-50
    fund = 25 + aflow_ratio * 25
    fund = max(0, min(50, fund))

    # B 價量分 0-20
    pv = 0
    if avgp and price >= avgp:
        pv += 10  # 站上均價
    if vr >= 1.5:
        pv += 7
    elif vr >= 1.2:
        pv += 4
    elif vr >= 1.0:
        pv += 2
    # 漲跌加成
    if chg >= 1.5:
        pv += 3
    elif chg >= 0.5:
        pv += 1
    pv = max(0, min(20, pv))

    # C 族群分 0-5
    rs = chg - sec_pct
    if rs >= 1.5:
        sec_score = 5
    elif rs >= 0.5:
        sec_score = 3
    elif rs >= 0:
        sec_score = 1
    else:
        sec_score = 0
    sec_score = max(0, min(5, sec_score))

    # D Chip Score 0-25
    ch = s.get("_chip_raw") or {}
    cs = chip_score(ch)
    chip_pts = cs["score"] if cs["score"] is not None else 0

    total = fund + pv + sec_score + chip_pts
    score = int(max(0, min(100, total)))

    # 把公式分項掛到 _health 供 UI 顯示「為什麼這分」
    new_h = dict(h)
    new_h["health_score"] = score
    new_h["health_v3_breakdown"] = {
        "fund": round(fund, 1),
        "pv": round(pv, 1),
        "sector": sec_score,
        "chip": chip_pts,
        "chip_reason": cs.get("reason", ""),
        "chip_level": cs.get("level", "no_data"),
    }
    s["_health"] = new_h
    return new_h


def annotate(snaps, sectors, market_pct=0.0):
    """
    原地為每檔 snap 增補 _health / _tri;為 sectors 增補 _health。
    回傳 (sector_health_map, verdict_counts)
    v3 升級:在原 annotate 流程後,跑 chip_score + 重算 health_score(v3 公式)。
    """
    sec_pct = {s["name"]: s["pct"] for s in (sectors or [])}
    by_sec = {}
    for s in snaps:
        s["_health"] = stock_health(s)
        # 把族群 pct 預先掛上,recompute_health_score 要用
        s["_sector_pct"] = sec_pct.get(s.get("sector"), 0)
        by_sec.setdefault(s.get("sector"), []).append(s)

    counts = {"bullish": 0, "bearish": 0, "pending": 0}
    for s in snaps:
        ch = None
        try:
            import chips
            ch = chips.get_chips(s["code"])
        except Exception:
            pass
        # 把真籌碼掛到個股,供前端顯示(無資料明確標 None → 前端顯示「無資料」)
        s["_chip_raw"] = ch or {}
        s["_chip"] = {
            "inst_net_20d_lots": (ch or {}).get("inst_net_20d_lots"),
            "inst_streak": (ch or {}).get("inst_streak"),
            "big_holder_pct": (ch or {}).get("big_holder_pct"),
            "big_holder_trend": (ch or {}).get("big_holder_trend"),
            "has_data": bool(ch and ch.get("inst_net_20d_lots") is not None),
            "chip_score": chip_score(ch or {}),
        }
        ev = gather_evidence(s, s["_health"], sec_pct.get(s.get("sector"), 0),
                             market_pct, ch)
        # 把證據原始 list 掛在 s 上,供 API 吐給 UI 顯示「為什麼這個分數」
        s["_ev"] = [{"cat": c, "dir": d, "why": w} for c, d, w in ev]
        s["_tri"] = triangulate(ev)
        counts[s["_tri"]["verdict"]] += 1

    # v3 升級:依新公式重算 health_score(在 _decision 之前)
    for s in snaps:
        recompute_health_score(s)

    sh_map = {}
    for name, members in by_sec.items():
        if name:
            sh_map[name] = sector_health(name, members)
    return sh_map, counts
