"""
MLS 插件 — money_health.py
資金健康度引擎 v2.2:四模組健康分 + Level 8.1 證據三角交叉驗證
====================================================================
純插件:只讀主系統資料,不改任何主邏輯。供 /api/state 增補 health 欄位、
供 nexora 盤後報告呼叫。

v2.2 優化(對應使用者診斷「健康分只依賴價格與簡化籌碼指標」):
  舊版 health_score 只有「資金方向 + 漲跌幅」單薄公式。
  新版 health_score = 四模組加權合成,不再只看價格:

    A. Price      均線/突破/量比/漲跌幅                  預設權重 0.30
    B. MoneyFlow  主動淨流方向 + 流速(scoring.flow_velocity)  預設權重 0.30
    C. Chip       法人/大戶(chip_provider,quality 誠實標記)  預設權重 0.20
    D. Sector     族群相對強弱 + 族群內排名                預設權重 0.20

  C 模組若完全無資料(FinMind 也查無),四模組自動降為三模組,
  權重按比例重分配到 A/B/D,不會用假分數硬湊。
  quality 欄位('finmind_basic'/'premium')會原樣透出到 API/報告,
  避免「近月+19,878張」這種法人日資料摘要被誤讀成分點級籌碼分析。

四象限分類(市場狀態分類,不是買賣訊號,鐵律照舊):
  健康(in_up)   資金流入 + 上漲       → 真攻擊
  假紅(in_down) 資金流入 + 下跌       → 邊拉邊賣/砸盤被算主動買
  惜售(out_up)  資金流出 + 上漲       → 量縮惜售,續航存疑
  休息(out_down)資金流出 + 下跌       → 輪動退潮

Level 8.1:決策前強制三根異質證據(A資金/B價量/C技術/D結構/E外部),
  同源不算;三方矛盾 → 強制「等待確認」並輸出下一驗證訊號。
"""

from statistics import median

import config as C
import scoring
import chip_provider

HEALTH_LABEL = {
    "in_up":    ("健康", "★★★★★", "資金流入且上漲,價量同向,真攻擊"),
    "in_down":  ("假紅", "★★☆☆☆", "資金流入但下跌,邊拉邊賣/砸盤疑慮,等外資蓋章"),
    "out_up":   ("惜售", "★★★☆☆", "資金流出但上漲,量縮惜售,續航存疑"),
    "out_down": ("休息", "★☆☆☆☆", "資金流出且下跌,輪動退潮,不接刀"),
}

# ── 四模組權重(對接只改這裡;Chip 缺資料時自動按比例重分配) ──
MODULE_WEIGHTS = {"price": 0.30, "flow": 0.30, "chip": 0.20, "sector": 0.20}


# ════════════════════════════════════════════════════════
# 模組 A:Price(均線/突破/量比/漲跌幅)
# ════════════════════════════════════════════════════════
def _price_score(s):
    price = s.get("price") or 0
    avgp = s.get("avg_price") or 0
    hi = s.get("high") or 0
    chg = s.get("change_rate") or 0
    vr = s.get("volume_ratio") or 0
    score = 50
    if avgp:
        score += 15 if price >= avgp else -15
    if hi and price >= hi and chg > 0:
        score += 15                              # 突破今高
    score += max(-15, min(15, chg * 3))           # 漲跌幅貢獻(±5%封頂)
    if vr >= 1.5:
        score += 10
    elif vr and vr < 0.8:
        score -= 5
    return int(max(0, min(100, score)))


# ════════════════════════════════════════════════════════
# 模組 B:MoneyFlow(主動淨流方向 + 流速)
# ════════════════════════════════════════════════════════
def _flow_score(ratio, velocity):
    score = 50 + ratio * 100
    score += max(-15, min(15, velocity * 80))     # 轉強加分/轉弱扣分
    return int(max(0, min(100, score)))


# ════════════════════════════════════════════════════════
# 模組 C:Chip(法人/大戶;quality 誠實標記,無資料時回 None)
# ════════════════════════════════════════════════════════
def _chip_score(chip, quality):
    if not chip:
        return None
    net = chip.get("inst_net_20d_lots")
    streak = chip.get("inst_streak")
    bh_trend = chip.get("big_holder_trend")
    broker_conc = chip.get("broker_concentration")     # premium 才有
    main_branch = chip.get("main_branch_net")           # premium 才有
    if net is None and streak is None and bh_trend is None \
            and broker_conc is None and main_branch is None:
        return None
    score = 50
    if net is not None:
        score += max(-20, min(20, net / 2000 * 20))
    if streak is not None:
        score += max(-15, min(15, streak * 3))
    if bh_trend is not None:
        score += max(-15, min(15, bh_trend * 10))
    if quality == "premium":
        if broker_conc is not None:
            score += max(-10, min(10, (broker_conc - 0.5) * 40))
        if main_branch is not None:
            score += max(-10, min(10, main_branch / 1000 * 10))
    return int(max(0, min(100, score)))


# ════════════════════════════════════════════════════════
# 模組 D:Sector(族群相對強弱 + 族群內排名)
# ════════════════════════════════════════════════════════
def _sector_score(chg, sector_pct, sector_rank, n_sectors):
    rs = (chg or 0) - (sector_pct or 0)
    score = 50 + max(-20, min(20, rs * 6))
    if sector_rank and n_sectors:
        score += max(-15, min(15, (n_sectors - sector_rank) / n_sectors * 30 - 15))
    return int(max(0, min(100, score)))


def _composite(price_s, flow_s, chip_s, sector_s):
    """chip 缺資料時,權重按比例重分配到其餘三模組。"""
    parts = {"price": price_s, "flow": flow_s, "sector": sector_s}
    if chip_s is not None:
        parts["chip"] = chip_s
    w = {k: MODULE_WEIGHTS[k] for k in parts}
    wsum = sum(w.values())
    w = {k: v / wsum for k, v in w.items()}
    return int(round(sum(parts[k] * w[k] for k in parts)))


def stock_health(s, sector_pct=None, market_pct=0.0,
                 sector_rank=None, n_sectors=None):
    """
    個股資金健康度(v2.2 四模組)。回傳 dict:
      quadrant, label, stars, desc, health_score(0-100,四模組合成),
      aflow_ratio, flow_velocity,
      module_scores: {price, flow, chip, sector}(chip 可能 None),
      chip_quality: 'finmind_basic' | 'premium'
    """
    aflow = scoring.get_aflow(s["code"])
    tv = s.get("total_volume") or 1
    ratio = aflow / tv                      # +主動買 / -主動賣
    scoring.push_flow_ratio(s["code"], ratio)
    velocity = scoring.flow_velocity(s["code"])
    chg = s.get("change_rate") or 0
    flow_in = ratio >= 0

    if flow_in and chg >= 0:   quad = "in_up"
    elif flow_in and chg < 0:  quad = "in_down"
    elif not flow_in and chg >= 0: quad = "out_up"
    else: quad = "out_down"
    label, stars, desc = HEALTH_LABEL[quad]

    chip, quality = chip_provider.get_chip_data(s["code"])

    price_s = _price_score(s)
    flow_s = _flow_score(ratio, velocity)
    chip_s = _chip_score(chip, quality)
    sector_s = _sector_score(chg, sector_pct, sector_rank, n_sectors)
    score = _composite(price_s, flow_s, chip_s, sector_s)

    return {"quadrant": quad, "label": label, "stars": stars, "desc": desc,
            "health_score": score, "aflow_ratio": round(ratio, 3),
            "flow_velocity": velocity,
            "module_scores": {"price": price_s, "flow": flow_s,
                              "chip": chip_s, "sector": sector_s},
            "chip_quality": quality}


def sector_health(sector_name, members):
    """族群資金健康度:成員健康分中位 + 族群流向×漲跌象限。"""
    if not members:
        return None
    scores = [m["_health"]["health_score"] for m in members if m.get("_health")]
    med = median(scores) if scores else 50
    # 族群層象限用成員 aflow 加總 vs 漲幅中位
    net = sum(scoring.get_aflow(m["code"]) for m in members)
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
# 主入口:為一批 snapshot 增補 health + triangulation
# ════════════════════════════════════════════════════════
def annotate(snaps, sectors, market_pct=0.0):
    """
    原地為每檔 snap 增補 _health / _tri;為 sectors 增補 _health。
    回傳 (sector_health_map, verdict_counts)
    """
    sec_pct = {s["name"]: s["pct"] for s in (sectors or [])}
    by_sec = {}
    for s in snaps:
        s["_health"] = stock_health(s)
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
        s["_chip"] = {
            "inst_net_20d_lots": (ch or {}).get("inst_net_20d_lots"),
            "inst_streak": (ch or {}).get("inst_streak"),
            "big_holder_pct": (ch or {}).get("big_holder_pct"),
            "big_holder_trend": (ch or {}).get("big_holder_trend"),
            "has_data": bool(ch and ch.get("inst_net_20d_lots") is not None),
        }
        ev = gather_evidence(s, s["_health"], sec_pct.get(s.get("sector"), 0),
                             market_pct, ch)
        s["_tri"] = triangulate(ev)
        counts[s["_tri"]["verdict"]] += 1

    sh_map = {}
    for name, members in by_sec.items():
        if name:
            sh_map[name] = sector_health(name, members)
    return sh_map, counts
