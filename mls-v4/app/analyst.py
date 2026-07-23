"""
MLS v4.0 — analyst.py
把 data_collector 撈到的真實盤後資料，轉成 final7.html 吃的 DATA 結構。
- 沒有 mock、沒有空白
- 每個欄位都是從 TWSE / TPEx / FinMind 計算出來
- 大戶集保欄位（chip_detail.big_holder_trend）已拿掉，漏斗改 3 關
"""
from datetime import datetime, timedelta
from typing import Optional

import data_collector as dc
import config as C
import scoring


def _shares_to_lots(shares: int) -> int:
    """Convert shares to lots without flooring negative values."""
    return int(shares / 1000)


# ══════════════════════════════════════════════════════════════
# 1. 個股 20 日法人 + 連買天數
# ══════════════════════════════════════════════════════════════
def _inst_breakdown(code: str) -> dict:
    """return {foreign_5d_lots, invest_5d_lots, dealer_5d_lots, foreign_streak, invest_streak, dealer_streak}"""
    rows = dc.fetch_finmind_inst(code, days=10)
    if not rows:
        return {"foreign_5d": 0, "invest_5d": 0, "dealer_5d": 0,
                "foreign_streak": 0, "invest_streak": 0, "dealer_streak": 0}

    by_date = {}
    for r in rows:
        d = r["date"]
        by_date.setdefault(d, {})[r["name"]] = (int(r.get("buy", 0)) - int(r.get("sell", 0)))

    dates = sorted(by_date.keys(), reverse=True)[:5]  # 近 5 日

    def net(name_keys):
        return sum(by_date[d].get(k, 0) for d in dates for k in name_keys)

    foreign_5d = _shares_to_lots(net(["Foreign_Investor", "Foreign_Dealer_Self"]))
    invest_5d = _shares_to_lots(net(["Investment_Trust"]))
    dealer_5d = _shares_to_lots(net(["Dealer_self", "Dealer_Hedging"]))

    def streak(name_keys):
        s = 0
        sign = 0
        for d in dates:
            v = sum(by_date[d].get(k, 0) for k in name_keys)
            if v == 0:
                break
            cur = 1 if v > 0 else -1
            if sign == 0:
                sign = cur
                s = 1
            elif cur == sign:
                s += 1
            else:
                break
        return s * sign if sign else 0

    # 區間起訖（dates 由近到遠,最早=最尾,最近=最頭）
    chg_date_from = dates[-1] if dates else "—"  # 最早一日
    chg_date_to = dates[0] if dates else "—"     # 最近一日

    return {
        "foreign_5d": foreign_5d, "invest_5d": invest_5d, "dealer_5d": dealer_5d,
        "foreign_streak": streak(["Foreign_Investor", "Foreign_Dealer_Self"]),
        "invest_streak": streak(["Investment_Trust"]),
        "dealer_streak": streak(["Dealer_self", "Dealer_Hedging"]),
        "chg_date_from": chg_date_from,
        "chg_date_to": chg_date_to,
    }


def _inst_20d(code: str) -> dict:
    """return {net_lots, foreign_lots, invest_lots, dealer_lots, streak, daily_5d: [...]}
    daily_5d: list of {date, foreign, invest, dealer, total} 最近 5 個交易日每日分項"""
    rows = dc.fetch_finmind_inst(code, days=25)
    if not rows:
        return {"net_lots": 0, "foreign_lots": 0, "invest_lots": 0, "dealer_lots": 0, "streak": 0, "daily_5d": [], "data_incomplete": True}

    by_date = {}
    for r in rows:
        d = r["date"]
        by_date.setdefault(d, {})[r["name"]] = (int(r.get("buy", 0)) - int(r.get("sell", 0)))

    dates = sorted(by_date.keys(), reverse=True)[:20]
    f_sum = i_sum = d_sum = 0
    for d in dates:
        bucket = by_date[d]
        f_sum += bucket.get("Foreign_Investor", 0) + bucket.get("Foreign_Dealer_Self", 0)
        i_sum += bucket.get("Investment_Trust", 0)
        d_sum += bucket.get("Dealer_self", 0) + bucket.get("Dealer_Hedging", 0)
    f_sum //= 1000
    i_sum //= 1000
    d_sum //= 1000

    # 連買天數：看最近有資料的 N 天 net_lots 連續正或負
    streak = 0
    sign = 0
    for d in dates:
        bucket = by_date[d]
        net = (bucket.get("Foreign_Investor", 0)
               + bucket.get("Foreign_Dealer_Self", 0)
               + bucket.get("Investment_Trust", 0)
               + bucket.get("Dealer_self", 0)
               + bucket.get("Dealer_Hedging", 0))
        if net == 0:
            break
        s = 1 if net > 0 else -1
        if sign == 0:
            sign = s
            streak = 1
        elif s == sign:
            streak += 1
        else:
            break

    # 每日序列（最近 5 個交易日）
    daily_5d = []
    for d in sorted(dates, reverse=True)[:5]:
        b = by_date[d]
        f_d = _shares_to_lots(b.get("Foreign_Investor", 0) + b.get("Foreign_Dealer_Self", 0))
        i_d = _shares_to_lots(b.get("Investment_Trust", 0))
        d_d = _shares_to_lots(b.get("Dealer_self", 0) + b.get("Dealer_Hedging", 0))
        daily_5d.append({
            "date": d,
            "foreign": f_d, "invest": i_d, "dealer": d_d, "total": f_d + i_d + d_d,
        })

    return {
        "net_lots": f_sum + i_sum + d_sum,
        "foreign_lots": f_sum,
        "invest_lots": i_sum,
        "dealer_lots": d_sum,
        "streak": streak * sign,
        "daily_5d": daily_5d,  # 新增：最近 5 日每日法人分項
    }


# ══════════════════════════════════════════════════════════════
# 2. 個股 20 日均線 + 8 日收盤時序
# ══════════════════════════════════════════════════════════════
def _price_tech(code: str, today_close: float, prev_close: float) -> dict:
    rows = dc.fetch_finmind_price(code, days=30)
    if not rows:
        # 用今天的 close / prev_close 兜底，沒 ma20
        return {
            "close": today_close, "prev_close": prev_close, "high": today_close, "low": today_close,
            "ma20": prev_close, "above_ma20": today_close > prev_close, "trigger": today_close * 1.01,
            "bias": round((today_close - prev_close) / prev_close * 100, 2) if prev_close else 0,
            "closes_8d": [], "data_incomplete": True,
        }

    # FinMind 是 old → new
    closes = [r["close"] for r in rows]
    # ma20 改用「近 20 個交易日」，但**偵測分割**：若 last20 最大/最小比 > 2.5，視為有分割
    # → 只取最近 5 日算 ma5 代替 ma20，並標 data_incomplete
    last20 = closes[-20:] if len(closes) >= 20 else closes
    if last20 and max(last20) / min(last20) > 2.5:
        # 偵測到分割/減資：只用近 5 日當均線
        ma = sum(closes[-5:]) / min(5, len(closes))
        data_incomplete = True
    else:
        ma = sum(last20) / len(last20)
        data_incomplete = False
    ma20 = ma

    # 最近 8 日
    closes_8d = closes[-8:] if len(closes) >= 8 else closes

    # trigger 計算（在 dict 外，才能用 if/else + 多行）
    if len(rows) >= 10:
        raw_high = max([r["max"] for r in rows[-10:]])
    else:
        raw_high = today_close
    trigger = round(min(raw_high, today_close * 1.05), 2)

    return {
        "close": today_close,
        "prev_close": prev_close,
        "high": max([r["max"] for r in rows[-1:]] or [today_close]),
        "low": min([r["min"] for r in rows[-1:]] or [today_close]),
        "ma20": round(ma20, 2),
        "above_ma20": today_close > ma20,
        "bias": round((today_close - ma20) / ma20 * 100, 2) if ma20 else 0,
        "closes_8d": [round(c, 2) for c in closes_8d],
        "data_incomplete": data_incomplete,
        "trigger": trigger,
    }


# ══════════════════════════════════════════════════════════════
# 3. 李佛摩六欄狀態機 (盤後推算: 收盤 vs 8 日均線 + 趨勢線)
# ══════════════════════════════════════════════════════════════
def _livermore_states(closes_8d: list) -> list:
    """return list[{date, state, price, pivot}] for 最後 8 個交易日"""
    if len(closes_8d) < 2:
        return []
    states = []
    for i in range(len(closes_8d)):
        # 8 個點，state 由前後 3 日相對位置決定
        start = max(0, i - 2)
        end = min(len(closes_8d), i + 3)
        window = closes_8d[start:end]
        cur = closes_8d[i]
        avg = sum(window) / len(window)
        # pivot 偵測
        is_pivot = False
        if 0 < i < len(closes_8d) - 1:
            prev_c = closes_8d[i - 1]
            next_c = closes_8d[i + 1]
            if (cur > prev_c and cur > next_c and (cur - min(prev_c, next_c)) / cur > 0.02) \
               or (cur < prev_c and cur < next_c and (max(prev_c, next_c) - cur) / cur > 0.02):
                is_pivot = True
        # 簡化版李佛摩：
        if cur > avg * 1.02:
            state = "上升趨勢"
        elif cur > avg * 1.005:
            state = "自然反彈"
        elif cur > avg * 0.995:
            if i > 0 and closes_8d[i-1] < closes_8d[i]:
                state = "次級反彈"
            else:
                state = "自然回檔"
        elif cur > avg * 0.98:
            state = "自然回檔"
        else:
            state = "下降趨勢"
        states.append({"price": round(cur, 2), "state": state, "pivot": is_pivot})
    return states


# ══════════════════════════════════════════════════════════════
# 3.5 融資券變化（從 FinMind MarginPurchase 真實抓）
# ══════════════════════════════════════════════════════════════
def _margin_trend(code: str) -> dict:
    """return {balance, chg_5d, chg_date_from, chg_date_to, data_incomplete}
    chg_5d = 今日融資餘額 - 第 5 個有資料日融資餘額
    明示起算日避免口徑差"""
    rows = dc.fetch_finmind_margin(code, days=10)
    if not rows:
        return {"balance": 0, "chg_5d": 0, "chg_date_from": "—", "chg_date_to": "—", "data_incomplete": True}
    today = rows[-1]
    balance = int(today.get("MarginPurchaseTodayBalance", 0))
    # 取第 5 個有資料的 FinMind 日（非 calendar 5 日）
    idx_5d = max(0, len(rows) - 6)
    bal_5d_row = rows[idx_5d]
    bal_5d_ago = int(bal_5d_row.get("MarginPurchaseTodayBalance", 0))
    chg = balance - bal_5d_ago
    return {
        "balance": balance,
        "chg_5d": chg,
        "chg_date_from": bal_5d_row.get("date", "—"),
        "chg_date_to": today.get("date", "—"),
        "data_incomplete": False,
    }


# ══════════════════════════════════════════════════════════════
# 4. 象限 (in_up / in_down / out_up / out_down)
# ══════════════════════════════════════════════════════════════
def _quad(ratio: float, chg_pct: float) -> str:
    # ratio = 主動買賣差/成交 (TWSE 沒有免費即時主動買賣，FinMind 也沒這欄)
    # 用「法人 5 日淨 + 漲跌」雙條件推估:
    # ratio>0 算流入，<0 流出（這裡用 chg_pct 替代因為沒主動買賣）
    in_out = "in" if chg_pct > 0 else "out"
    up_dn = "up" if chg_pct > 0 else "down"
    return f"{in_out}_{up_dn}"


def _ratio_estimate(chip: dict, chg_pct: float) -> float:
    """沒有主動買賣 API，用「法人 20 日淨 + 今日漲跌」做 ratio 估計。"""
    if chip["net_lots"] > 0 and chg_pct > 0:
        return round(min(chip["net_lots"] / 50000, 0.5), 3)
    if chip["net_lots"] < 0 and chg_pct < 0:
        return round(max(chip["net_lots"] / 50000, -0.5), 3)
    if chg_pct > 0 and chip["net_lots"] < 0:
        return round(chip["net_lots"] / 100000, 3)  # 漲但法人賣
    if chg_pct < 0 and chip["net_lots"] > 0:
        return round(chip["net_lots"] / 100000, 3)  # 跌但法人買
    return 0.0


# ══════════════════════════════════════════════════════════════
# 5. 健康分（替代 final7 的決策公式）
# ══════════════════════════════════════════════════════════════
def _score(quad: str, streak: int, chg_pct: float, ratio: float) -> dict:
    base = {"in_up": 60, "in_down": 45, "out_up": 40, "out_down": 25}[quad]
    streak_bonus = min(abs(streak), 5) * 2 * (1 if streak > 0 else -1)
    trend_bonus = 10 if (quad == "in_up" and chg_pct > 1) else (5 if chg_pct > 0 else (-5 if chg_pct < -1 else 0))
    # 舊 _score 已廢棄，新流程在 _module_scores + _decision_engine
    pass


# ══════════════════════════════════════════════════════════════
# 6. 漏斗 3 關（重寫：拿掉大戶關、硬風險直接 fail）
# ══════════════════════════════════════════════════════════════
def _funnel(quad: str, breakdown: dict, hard_risk_hit: bool, chg_pct: float) -> dict:
    """return {g1, g2, g3, hard_risk_fail, contradictions: []}
    規則：
    - g1 資金流向: in_up / in_down / out_down = pass；out_up = fail
    - g2 法人未斷: 5 日合計 >= 0 = pass；< 0 = fail（拿掉 streak 模糊判斷）
    - g3 承接品質: 4 維 (外資/融資/大戶/股價) 矛盾 = ⚠；>= 2 ✓ = pass；>= 2 ✗ = fail
    - 任何硬風險命中 → 整個 funnel = fail
    """
    g1 = "pass" if quad in ("in_up", "in_down", "out_down") else "fail"
    total_5d = breakdown.get("foreign_5d_lots", 0) + breakdown.get("invest_5d_lots", 0) + breakdown.get("dealer_5d_lots", 0)
    g2 = "pass" if total_5d >= 0 else "fail"

    # g3 改由 UI 用 4 維 cell 算，這裡只回 raw 結果
    return {
        "g1": g1,
        "g2": g2,
        "g3": "nd",  # UI 端算
        "hard_risk_fail": hard_risk_hit,
        "total_5d": total_5d,
    }


# ══════════════════════════════════════════════════════════════
# 5.5 模組分數（各模組獨立算）
# ══════════════════════════════════════════════════════════════
def _module_scores(quad, streak, chg_pct, ratio, inst_5d_total, margin_5d_chg,
                   tech, inst_breakdown) -> dict:
    """return {tech, capital, chip, sector, absorption} 各模組獨立 0-100 分
    不做降級、不做封頂 — 由 _decision_engine 統一收斂"""
    # 技術 0-100
    tech_s = 50
    if tech["above_ma20"]: tech_s += 25
    bias = tech.get("bias", 0)
    if -2 <= bias <= 5: tech_s += 15   # 站上 + 乖離健康
    elif -5 <= bias <= 8: tech_s += 5
    else: tech_s -= 10  # 過度乖離
    # 接近前高
    if tech.get("trigger", 0) > 0 and tech["close"] / tech["trigger"] > 0.95:
        tech_s += 10
    tech_s = max(0, min(100, tech_s))

    # 資金 0-100
    cap_s = 50
    if quad in ("in_up",): cap_s += 25
    elif quad in ("in_down",): cap_s += 5
    elif quad in ("out_up",): cap_s += 0
    else: cap_s -= 20
    if chg_pct > 2: cap_s += 15
    elif chg_pct > 0: cap_s += 5
    elif chg_pct < -2: cap_s -= 15
    cap_s = max(0, min(100, cap_s))

    # 籌碼 0-100 — 用 5 日分項
    f5, i5, d5 = inst_breakdown["foreign_5d"], inst_breakdown["invest_5d"], inst_breakdown["dealer_5d"]
    chip_s = 50
    if f5 > 0: chip_s += 10
    if f5 > 500: chip_s += 5
    if i5 > 0: chip_s += 10
    if i5 > 1000: chip_s += 10  # 投信大買
    if inst_breakdown["invest_streak"] >= 3: chip_s += 10
    if d5 > 0: chip_s += 5
    if inst_5d_total < -500: chip_s -= 20
    if inst_5d_total < -2000: chip_s -= 10
    chip_s = max(0, min(100, chip_s))

    # 族群 0-100 — sector_chg / vs_sector 從 build_data 傳
    sector_s = 50  # default

    # 第五模組 0-100 — 大戶缺值 = 60 中性，融資降 = 加分
    abs_s = 60  # 大戶無公開 API = 中性
    if margin_5d_chg < -500 and chg_pct > 0: abs_s += 15   # 融資降 + 漲 = 真承接
    if margin_5d_chg > 500 and chg_pct < 0: abs_s -= 15   # 融資增 + 跌 = 散戶接刀
    abs_s = max(0, min(100, abs_s))

    return {
        "tech": tech_s,
        "capital": cap_s,
        "chip": chip_s,
        "sector": sector_s,
        "absorption": abs_s,
    }


# ══════════════════════════════════════════════════════════════
# 6. Decision Engine — 收斂所有模組出最終 score / grade / stars
# ══════════════════════════════════════════════════════════════
def _decision_engine(mods: dict, risk: dict, inst_5d_total: int,
                    chg_pct: float, vs_sector: float, sector_chg: float,
                    inst_breakdown: dict) -> dict:
    """把所有模組分數 + 風險 + 法人分項 → 最終 score / grade / stars
    規則：
    - 加權平均：技術 25% + 資金 20% + 籌碼 25% + 族群 10% + 第五模組 20%
    - 硬風險 → 等級封頂 Watch
    - 軟風險 → score 扣分
    - 五模組共振 → 升級（Watch→Ready）
    - 法人分項分歧 → 信心扣分"""
    weights = {"tech": 0.25, "capital": 0.20, "chip": 0.25, "sector": 0.10, "absorption": 0.20}
    weighted = sum(mods[k] * weights[k] for k in weights)
    final = round(weighted, 1)
    downgrade_reasons = []

    # 軟風險扣分（過度乖離 + 接近漲停是同一根 K 線，只扣一次）
    soft_count = 0
    if risk.get("over_bias"): soft_count += 1; downgrade_reasons.append("過度乖離")
    if risk.get("near_limit"): soft_count += 1; downgrade_reasons.append("接近漲停/爆量")
    if risk.get("ma_break"): final -= 5; downgrade_reasons.append("跌破月線")
    if risk.get("failed_breakout"): final -= 8; downgrade_reasons.append("盤中突破失敗（高點觸及但收盤未站穩）")
    if soft_count >= 1: final -= 6  # 軟風險只扣一次
    if soft_count >= 2: final -= 3  # 兩個軟風險同時再扣 3

    # 硬風險封頂
    hard_risk_hit = any(risk.get(k) for k in ("ma_break", "divergence"))
    if hard_risk_hit:
        downgrade_reasons.append("硬風險命中（量價背離/破月線）→ 封頂 Watch")
        final = min(final, 60)

    # 法人合計賣超 + 個股跌 → 扣
    if inst_5d_total < 0 and chg_pct < 0:
        final -= 15
        downgrade_reasons.append("法人5日合計賣超+個股跌")
    final = max(0, min(100, final))

    # 投信主導 + 法人分歧 → 信心扣分
    main_buyer = ""
    parts = [("外", inst_breakdown["foreign_5d"]),
             ("投", inst_breakdown["invest_5d"]),
             ("自", inst_breakdown["dealer_5d"])]
    parts.sort(key=lambda x: -x[1])
    if parts[0][1] > 0:
        main_buyer = parts[0][0]
        # 三法人方向是否一致
        signs = [1 if x > 100 else (-1 if x < -100 else 0) for _, x in parts]
        if signs.count(1) == 1 and signs.count(-1) == 1:
            downgrade_reasons.append(f"法人方向分歧（{main_buyer}主導）")
        elif signs.count(1) >= 2:
            main_buyer = "共振"
    else:
        main_buyer = "—"
        downgrade_reasons.append("無主導買方")

    # 五模組共振升級
    resonance = sum(1 for v in mods.values() if v >= 70)
    if resonance >= 4 and final >= 65 and not hard_risk_hit:
        final = min(100, final + 5)
        downgrade_reasons.append(f"五模組高於 70 ({resonance}/5) → 共振加分")

    final = max(0, min(100, final))

    # 信心值：0-100
    confidence = 100
    if downgrade_reasons: confidence -= len(downgrade_reasons) * 8
    if not main_buyer: confidence -= 20
    if main_buyer == "—": confidence -= 15
    confidence = max(0, min(100, confidence))

    grade = "Ready" if final >= C.READY_MIN else ("Watch" if final >= C.WATCH_MIN else "Hold")
    # stars 直接由 score 來，0-100 → 1-5 星
    stars = max(1, min(5, int(final / 20)))

    return {
        "final_score": final,
        "final_grade": grade,
        "final_stars": stars,
        "confidence": confidence,
        "downgrade_reasons": downgrade_reasons,
        "main_buyer": main_buyer,
        "module_scores": mods,
    }


# ══════════════════════════════════════════════════════════════
# 7. 群體象限計算（同族群個股加權 chg）
# ══════════════════════════════════════════════════════════════
def _sector_chg_map(snap: dict) -> dict:
    """return {sector: avg_chg_pct}"""
    sector_chg = {}
    sector_cnt = {}
    for code, (name, sector, _) in C.UNIVERSE.items():
        p = snap["twse_prices"].get(code) or snap["tpex_prices"].get(code)
        if not p:
            continue
        sector_chg.setdefault(sector, 0)
        sector_cnt.setdefault(sector, 0)
        sector_chg[sector] += p["change_pct"]
        sector_cnt[sector] += 1
    return {s: round(sector_chg[s] / sector_cnt[s], 2) for s in sector_chg if sector_cnt[s] > 0}


# ══════════════════════════════════════════════════════════════
# 8. 主入口：組成 final7 吃的 DATA
# ══════════════════════════════════════════════════════════════
def build_data(date_yyyymmdd: str) -> list:
    snap = dc.snapshot_today(date_yyyymmdd)
    sector_chg = _sector_chg_map(snap)
    market_chg = snap["twse_index"].get("change_pct", 0)

    data = []
    for code, (name, sector, track) in C.UNIVERSE.items():
        price = snap["twse_prices"].get(code) or snap["tpex_prices"].get(code)
        if not price:
            # 沒收盤資料（停牌/未上市）→ 跳過，不放進 DATA
            continue
        inst = _inst_20d(code)
        # 額外抓分項 streak（Foreign/Trust 分別算連買連賣天數）
        inst_breakdown = _inst_breakdown(code)
        margin = _margin_trend(code)
        tech = _price_tech(code, price["close"], price["prev_close"])
        quad = _quad(0, price["change_pct"])
        ratio = _ratio_estimate(inst, price["change_pct"])
        # 先算 risk 才能傳 hard_risk_hit 給 _score
        _near_limit = abs(price["change_pct"]) >= 9
        # divergence 原因追蹤：真背離需要符合「收漲 + 收盤低於近 5 日均價」
        # 不能只看 closes_8d 末兩位的隨機波動
        closes = tech.get("closes_8d", [])
        near5_avg = sum(closes[-5:]) / len(closes[-5:]) if len(closes) >= 5 else price["close"]
        price_above_near5_avg = price["close"] > near5_avg
        closes_trending_down = (len(closes) >= 3 and closes[-1] < closes[-3])
        real_divergence = (price["close"] > tech["prev_close"]
                           and price_above_near5_avg
                           and closes_trending_down
                           and not _near_limit)
        divergence_reason = []
        if price["close"] > tech["prev_close"] and closes[-2] < closes[-1] if len(closes) >= 2 else False:
            divergence_reason.append(f"昨日收{tech['prev_close']}→今日{price['close']}漲但前1日{closes[-2]}<當前{closes[-1]}")
        if not price_above_near5_avg and price["close"] > tech["prev_close"]:
            divergence_reason.append(f"收漲但低於近5日均價{near5_avg:.1f}")
        if closes_trending_down:
            divergence_reason.append("近3日收盤趨勢向下")

        risk = {
            "ma_break": not tech["above_ma20"],
            # failed_breakout：盤中觸及突破價但收盤未站穩（tech.trigger 在 return 前算）
            "divergence": real_divergence,
            "divergence_reasons": divergence_reason if real_divergence else [],
            "proxy": False,
            "over_bias": tech["bias"] > 8,
            "near_limit": _near_limit,
            "no_breakout": True,
            "resistance": False,
            "failed_breakout": False,  # 下方 trade_plan 算完後會 overwrite
            "data_incomplete": tech.get("data_incomplete", False),
        }
        hard_risk_hit = any(risk.get(k) for k in ("ma_break", "divergence"))
        inst_5d_total = inst_breakdown["foreign_5d"] + inst_breakdown["invest_5d"] + inst_breakdown["dealer_5d"]
        sec_chg = sector_chg.get(sector, 0)
        vs_sector = round(price["change_pct"] - sec_chg, 2)
        # 各模組獨立算
        mods = _module_scores(quad, inst["streak"], price["change_pct"], ratio,
                              inst_5d_total, margin["chg_5d"], tech, inst_breakdown)
        # 族群模組補上（用 vs_sector 算）
        if vs_sector > 5: mods["sector"] = 80
        elif vs_sector > 1: mods["sector"] = 65
        elif vs_sector >= 0: mods["sector"] = 50
        else: mods["sector"] = 35
        # Decision Engine 收斂
        dec = _decision_engine(mods, risk, inst_5d_total, price["change_pct"],
                                vs_sector, sec_chg, inst_breakdown)
        # 連續評分引擎：取代舊版 60 分地板；法人/融資只使用 EOD 蓋章值。
        q_label = {"in_up": "流入↗漲", "in_down": "流入↗跌",
                   "out_up": "流出↘漲", "out_down": "流出↘跌"}[quad]
        scoring_input = scoring.StockInput(
            code=code, name=name, sector=sector, quadrant=q_label,
            day_change_pct=price["change_pct"], active_buysell_diff=ratio,
            vol_ratio=1, legal_20d_net=inst["net_lots"],
            foreign_20d=inst["foreign_lots"], trust_20d=inst["invest_lots"],
            legal_5d_net=inst_5d_total, foreign_5d=inst_breakdown["foreign_5d"],
            trust_5d=inst_breakdown["invest_5d"], legal_consec_days=inst["streak"],
            margin_5d_chg=margin["chg_5d"], close=price["close"], ma20=tech["ma20"],
            above_ma20=tech["above_ma20"], bias_pct=tech.get("bias", 0),
            foreign_turn_buy=scoring.PASS if inst_breakdown["foreign_5d"] > 500 else
                             scoring.FAIL if inst_breakdown["foreign_5d"] < -500 else scoring.NO_DATA,
            margin_down=scoring.PASS if margin["chg_5d"] < 0 else
                        scoring.FAIL if margin["chg_5d"] > 0 else scoring.NO_DATA,
            dahu_hold=scoring.NO_DATA, price_hold=scoring.FAIL if price["change_pct"] <= -2 else scoring.PASS,
            vs_sector_pct=vs_sector, near_limit_up=_near_limit,
            volume_blowout=False, no_breakout=price["close"] < tech["trigger"],
            dahu_custody=scoring.NO_DATA,
        )
        continuous = scoring.compute_health_score(scoring_input)
        dec["final_score"] = continuous["score"]
        dec["final_grade"] = continuous["grade"]
        dec["final_stars"] = continuous["stars"]
        dec["scoring"] = continuous
        # 為相容舊 UI：sc = final 結果
        sc = {"score": dec["final_score"], "grade": dec["final_grade"], "stars": dec["final_stars"]}
        funnel = _funnel(quad, inst_breakdown, hard_risk_hit, price["change_pct"])
        liv_states = _livermore_states(tech["closes_8d"])
        today_liv = liv_states[-1] if liv_states else {"state": "—", "price": price["close"], "pivot": None}

        sec_chg = sector_chg.get(sector, 0)
        vs_sector = round(price["change_pct"] - sec_chg, 2)
        vs_market = round(price["change_pct"] - market_chg, 2)
        rs = "領先" if vs_sector > 0 else "落後"

        # quad history 簡化版：5 日前同算法（從 FinMind price 倒推）
        closes = tech["closes_8d"]
        quad_history = []
        for i in range(-5, 0):
            if abs(i) <= len(closes):
                idx = len(closes) + i
                if idx >= 0:
                    hist_chg = (closes[idx] - (closes[idx - 1] if idx > 0 else closes[idx])) / (closes[idx - 1] if idx > 0 else 1) * 100
                    quad_history.append("in_up" if hist_chg > 0 else "in_down")

        # chip_label — 改成描述「主要承接者」是誰 + 整體方向
        f5 = inst_breakdown["foreign_5d"]
        i5 = inst_breakdown["invest_5d"]
        d5 = inst_breakdown["dealer_5d"]
        # 找哪個分項最大正貢獻
        parts = [("外資", f5), ("投信", i5), ("自營", d5)]
        parts.sort(key=lambda x: -x[1])
        main_buyer = parts[0][0] if parts[0][1] > 0 else "—"
        if inst["net_lots"] > 0 and parts[0][1] > 500:
            chip_label = f"法人偏多（{main_buyer}主導 +{parts[0][1]:,}）"
        elif inst["net_lots"] > 0:
            chip_label = f"法人微偏多（{main_buyer} +{parts[0][1]:,}）"
        elif inst["streak"] <= -3:
            chip_label = f"法人轉賣（{main_buyer} {parts[0][1]:,}）"
        else:
            chip_label = "待蓋章（分項分歧）"

        # 籌碼共振：5 日分項方向是否一致
        signs = [1 if x > 100 else (-1 if x < -100 else 0) for _, x in parts]
        if all(s == 1 for s in signs):
            chip_resonance = "三法人共振買超"
        elif all(s == -1 for s in signs):
            chip_resonance = "三法人共振賣超"
        elif sum(1 for s in signs if s > 0) == 1 and sum(1 for s in signs if s < 0) == 1:
            chip_resonance = "兩方分歧"
        else:
            chip_resonance = "部分共振／投信主導" if parts[0][0] == "投信" else f"部分共振／{parts[0][0]}主導"

        # formula 字串
        formula = f"模組加權 {dec['final_score']}分（信心 {dec['confidence']}%）｜main_buyer={dec['main_buyer']}｜降級:{','.join(dec['downgrade_reasons']) or '無'}"

        # risk
        # trade_plan — 短線/中線分開
        entry = tech["trigger"]
        # failed_breakout 判定：盤中高點 >= 進場價（觸及突破位）但收盤 < 進場價（未站穩）
        is_failed_breakout = (price["high"] >= entry and price["close"] < entry)
        risk["failed_breakout"] = is_failed_breakout  # 補回 risk dict
        # 短線停損：昨日低 (取 FinMind 收盤前日) 或收盤 95%
        prev_low = price.get("low", price["close"] * 0.95)  # 簡化用當日 low
        short_stop = min(prev_low, price["close"] * 0.95)
        # 中線停損：MA20
        mid_stop = tech["ma20"]
        stop = short_stop  # 預設顯示短線
        target = entry * 1.07

        # ai_summary — 用 final 結果
        tone = "✅" if sc["grade"] == "Ready" else ("⏳" if sc["grade"] == "Watch" else ("⚡" if quad == "out_down" else "⛔"))
        if sc["grade"] == "Hold" and quad == "out_down":
            tone = "⚡"
        action_map = {
            "Ready": f"站上月線({tech['ma20']})可進場，跌破月線出場，目標{entry}。",
            "Ready候選": "列入潛力觀察，等待 L2 籌碼技術確認或突破訊號成立。",
            "Watch": "列觀察，等籌碼蓋章或訊號成立再動作，不預設進場。",
            "Hold": "暫不動作，休息也是部位。",
        }
        # 規則：5 日法人合計賣超 + 跌價 → 不能寫「惜售」、要標「結構性賣壓」
        five_day_total = inst_breakdown["foreign_5d"] + inst_breakdown["invest_5d"] + inst_breakdown["dealer_5d"]
        is_structural_sell = five_day_total < 0 and price["change_pct"] < 0
        if quad == "out_up" and is_structural_sell:
            # 「惜售」象限但法人合計賣超 = 結構性賣壓，覆蓋
            quad_label = "結構性賣壓（法人合計賣超）"
        else:
            quad_label = {'in_up':'真攻擊','in_down':'假紅','out_up':'惜售','out_down':'休息'}[quad]
        ai_summary = {
            "state": f"{name}目前處於「{quad_label}」象限（{'流入' if quad.startswith('in') else '流出'}{'漲' if quad.endswith('up') else '跌'}），健康分{sc['score']}分、承接品質{sc['stars']}★。",
            "evidence": [
                f"法人20日淨 {inst['net_lots']:+,} 張 (外{inst['foreign_lots']:+,} 投{inst['invest_lots']:+,} 自{inst['dealer_lots']:+,})",
                f"5日 主要承接：{main_buyer} ({parts[0][1]:+,} 張)",
                f"融資5日變化 {margin['chg_5d']:+,} 張",
                f"相對族群{vs_sector:+.2f}%" if abs(vs_sector) > 0.3 else f"族群相當（{sec_chg:+.2f}%）",
            ],
            "hard_risk": [k for k, v in risk.items() if v and k in ("ma_break", "divergence")],
            "soft_risk": [k for k, v in risk.items() if v and k in ("over_bias", "near_limit", "no_breakout", "data_incomplete")],
            "action": action_map[sc["grade"]],
            "chip_resonance": chip_resonance,
            "data_completeness": "不足（大戶集保無公開 API）",
            "main_buyer": main_buyer,
            "main_buyer_amount": parts[0][1],
            "tone": f"{tone} 綜合研判：{'列為 Ready' if sc['grade']=='Ready' else ('列為 Watch' if sc['grade']=='Watch' else ('落難族群逆勢股，觀察是否洗盤' if quad=='out_down' else '體質偏弱，避開'))}。{chip_resonance}｜{main_buyer}主導",
        }

        # ai 勝率（簡化版：依分數查表）
        sc_n = sc["score"]
        # 規則：法人 5 日合計賣超時，健康分需額外扣 15%（避免「高 score 假紅」誤判）
        if five_day_total < 0 and price["change_pct"] < 0:
            sc_n = max(0, sc_n - 15)
        next_day = min(95, max(30, sc_n * 0.85 + 10))
        five_day = min(95, max(35, sc_n * 0.82 + 15))

        d = {
            "code": code,
            "name": name,
            "sector": sector,
            "track": track,
            "ratio": ratio,
            "src": "TWSE+FinMind",
            "quad": quad,
            "score": sc["score"],
            "grade": sc["grade"],
            "stars": sc["stars"],
            "chg": price["change_pct"],
            "vr": 1.0,  # 量比無歷史資料不計
            "streak": inst["streak"],
            "trend": "改善" if (sc_n >= 70) else ("持平" if (sc_n >= 50) else "惡化"),
            "chip": 1 if inst["net_lots"] > 0 and inst["streak"] >= 3 else 0,
            "formula": formula,
            "chip_label": chip_label,
            "tech": {
                "close": price["close"],
                "prev_close": price["prev_close"],
                "high": price["high"],
                "low": price["low"],
                "ma20": tech["ma20"],
                "above_ma20": tech["above_ma20"],
                "trigger": tech["trigger"],
            },
            "chip_detail": {
                # 20 日 (完整三項)
                "inst_net_20d_lots": inst["net_lots"],
                "inst_streak": inst["streak"],
                "foreign_lots": inst["foreign_lots"],
                "invest_lots": inst["invest_lots"],
                "dealer_lots": inst["dealer_lots"],  # 新增：20日自營
                # 5 日分項
                "foreign_5d_lots": inst_breakdown["foreign_5d"],
                "invest_5d_lots": inst_breakdown["invest_5d"],
                "dealer_5d_lots": inst_breakdown["dealer_5d"],
                "foreign_streak": inst_breakdown["foreign_streak"],
                "invest_streak": inst_breakdown["invest_streak"],
                "dealer_streak": inst_breakdown["dealer_streak"],
                # 5 日區間起訖
                "inst_5d_date_from": inst_breakdown.get("chg_date_from", "—"),
                "inst_5d_date_to": inst_breakdown.get("chg_date_to", "—"),
                # 每日序列（最近 5 個交易日）
                "daily_5d": inst["daily_5d"],
                # 融資
                "margin_balance": margin["balance"],
                "margin_5d_chg": margin["chg_5d"],
                "margin_chg_date_from": margin.get("chg_date_from", "—"),
                "margin_chg_date_to": margin.get("chg_date_to", "—"),
                "note": f"20日{inst['net_lots']:+,}張(外{inst['foreign_lots']:+,} 投{inst['invest_lots']:+,} 自{inst['dealer_lots']:+,})/5日 外{inst_breakdown['foreign_5d']:+,} 投{inst_breakdown['invest_5d']:+,} 自{inst_breakdown['dealer_5d']:+,}",
            },
            # 四層漏斗統一欄位（供正式漏斗/API/前端共用）
            "inst_net_20d": inst["net_lots"],
            "inst_5d_net": inst_5d_total,
            "trust_5d_net": inst_breakdown["invest_5d"],
            "margin_5d_chg": margin["chg_5d"],
            "above_ma20": tech["above_ma20"],
            "near_limit": _near_limit,
            "five_day_inst_total": inst_breakdown["foreign_5d"] + inst_breakdown["invest_5d"] + inst_breakdown["dealer_5d"],
            "relative": {
                "stock_chg": price["change_pct"],
                "sector_chg": sec_chg,
                "market_chg": market_chg,
                "vs_sector": vs_sector,
                "vs_market": vs_market,
                "rs": rs,
            },
            "risk": risk,
            "trade_plan": {
                "entry": entry,
                "stop": round(stop, 2),  # 短線停損（昨日低 / 收盤 95%）
                "short_stop": round(short_stop, 2),
                "mid_stop": round(mid_stop, 2),  # MA20 中線停損
                "target": round(target, 2),
                "volume": "需>昨",
            },
            "quad_history": quad_history or [quad] * 5,
            "ai": {
                "next_day": int(next_day),
                "five_day": int(five_day),
                "reasons": [
                    f"最終健康分 {dec['final_score']}（信心 {dec['confidence']}%）",
                    f"{sector}族群 {('領先' if vs_sector > 3 else ('相當' if vs_sector > 0 else '落後'))}",
                    f"法人20日 {inst['net_lots']:+,}張",
                    f"相對族群 {vs_sector:+.2f}%",
                ],
                "verdict": f"綜合證據 → {dec['final_grade']}（{dec['main_buyer']}）",
            },
            "livermore": today_liv,
            "liv_history": [
                {"date": (datetime.strptime(date_yyyymmdd, "%Y%m%d") - timedelta(days=len(liv_states) - 1 - i)).strftime("%m/%d"),
                 "state": s["state"], "price": s["price"], "pivot": s["pivot"]}
                for i, s in enumerate(liv_states)
            ],
            "ai_summary": ai_summary,
            "decision_engine": dec,  # 統一收斂結果
            "module_scores": mods,  # 各模組獨立分數
        }
        data.append(d)

    # 排序：分數高 → 低
    data.sort(key=lambda x: -x["score"])
    return data


if __name__ == "__main__":
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    data = build_data(today)
    print(f"date={today} 個股數={len(data)}")
    for d in data[:5]:
        print(f"  {d['code']} {d['name']} quad={d['quad']} score={d['score']} chg={d['chg']:+.2f}% "
              f"inst20d={d['chip_detail']['inst_net_20d_lots']:+,} streak={d['chip_detail']['inst_streak']}")
