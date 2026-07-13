"""
MLS 模組 — stock_card.py(v2.3 新增)
優化個股資訊卡:籌碼面 × 資金 × 技術 × 交易計畫 × AI 結論
====================================================================
組出前端資訊卡所需的完整 dict。每一格資料來源與週期誠實標記,
查無資料一律 None(前端顯示「—」),絕不假造。

區塊與資料來源:
  籌碼面   chips.get_chips_detail(外資/投信/自營=日資料;
           400張/千張大戶=集保週資料);主力分點=premium 介面,現階段 None
  資金     主動買/賣% = 快照 buy_volume/sell_volume(外/內盤累積);
           5日/10日資金流 = 日K帶方向量能(收漲日+量、收跌日−量)加總方向
  技術     indicators.py(MA5/10/20、MACD、KD、RSI、ATR;
           low 缺真值時 KD/ATR 標 approx)
  交易     以 ATR 建價位計畫:買點=觀察日高(突破觸發)、
           停損=買點−1.3×ATR、T1=買點+2×ATR、T2=買點+4×ATR、
           RR=(T1−買點)/(買點−停損)≈1.54(固定結構,ATR 缺值時整組 None)
  AI 結論  四模組健康分(money_health)映射 + ✓/✕ 原因清單
"""

from datetime import datetime, timezone, timedelta

import config as C
import indicators as I

try:
    import broker
except Exception:
    broker = None

TW_TZ = timezone(timedelta(hours=8))

STOP_ATR = 1.3      # 停損 = 買點 − 1.3×ATR
T1_ATR = 2.0        # 目標1 = 買點 + 2×ATR
T2_ATR = 4.0        # 目標2 = 買點 + 4×ATR


def _bars(code, days=80, injected=None):
    if injected is not None:
        return injected
    if broker is None:
        return []
    try:
        raw = broker.daily_kbars(code, days=days)
    except Exception as e:
        print(f"[stock_card] {code} 日K失敗:{e}")
        return []
    out = []
    for r in raw:
        cl = r.get("close")
        out.append({"date": str(r.get("date"))[:10], "close": cl,
                    "high": r.get("high"), "low": r.get("low"),
                    "volume": r.get("volume", 0)})
    return out


def _flow_days(bars, n):
    """近 n 日帶方向量能:收漲日 +volume、收跌日 −volume,回 ↑/↓/→。"""
    if len(bars) < n + 1:
        return None
    seg = bars[-(n + 1):]
    s = 0
    for a, b in zip(seg, seg[1:]):
        if a["close"] and b["close"] and b.get("volume"):
            s += b["volume"] if b["close"] > a["close"] else \
                (-b["volume"] if b["close"] < a["close"] else 0)
    return "↑" if s > 0 else ("↓" if s < 0 else "→")


def build_card(code, snap=None, health=None, grade=None,
               injected_bars=None, chip_detail=None):
    """
    組完整資訊卡。snap(盤中/收盤快照)、health(money_health 或
    dec_health 結果)、grade(Ready/Watch/Hold)由呼叫端提供可省 API;
    缺省時自行降級取得。
    """
    name = C.NAME_MAP.get(code, code)
    sector, styp = C.SECTOR_MAP.get(code, ("其他", "attack"))
    bars = _bars(code, injected=injected_bars)
    closes = [b["close"] for b in bars if b["close"] is not None]
    highs = [b["high"] if b["high"] is not None else b["close"] for b in bars]
    lows_raw = [b.get("low") for b in bars]
    low_approx = any(v is None for v in lows_raw) or not lows_raw
    lows = [(v if v is not None else b["close"])
            for v, b in zip(lows_raw, bars)]

    # ── 籌碼面 ──────────────────────────────────────────
    if chip_detail is None:
        try:
            import chips
            chip_detail = chips.get_chips_detail(code)
        except Exception as e:
            print(f"[stock_card] 籌碼細項失敗:{e}")
            chip_detail = {}
    cd = chip_detail or {}
    chip_block = {
        "foreign": cd.get("foreign_net_d"), "trust": cd.get("trust_net_d"),
        "dealer": cd.get("dealer_net_d"),
        "main_force": cd.get("main_force_net"),     # premium 才有,現為 None
        "big400_delta": cd.get("big400_delta"),
        "big1000_delta": cd.get("big1000_delta"),
        "big_holder_delta": cd.get("big400_delta"),  # 卡片「大戶持股」= 400張級距變化
        "period_note": "法人=T-1 盤後蓋章(非即時);大戶級距=集保週資料;主力分點=待接籌碼商",
    }

    # ── 資金 ────────────────────────────────────────────
    bv = (snap or {}).get("buy_volume") or 0
    sv = (snap or {}).get("sell_volume") or 0
    tot = bv + sv
    flow_block = {
        "active_buy_pct": round(bv / tot * 100, 1) if tot else None,
        "active_sell_pct": round(sv / tot * 100, 1) if tot else None,
        "flow_5d": _flow_days(bars, 5),
        "flow_10d": _flow_days(bars, 10),
    }

    # ── 技術 ────────────────────────────────────────────
    kd_v = I.kd(highs, lows, closes) if closes else None
    tech_block = {
        "ma5": I.ma_direction(closes, 5),
        "ma10": I.ma_direction(closes, 10),
        "ma20": I.ma_direction(closes, 20),
        "macd": (I.macd(closes) or {}).get("cross") if closes else None,
        "kd_k": kd_v[0] if kd_v else None,
        "kd_d": kd_v[1] if kd_v else None,
        "rsi": I.rsi(closes) if closes else None,
        "atr": I.atr(highs, lows, closes) if closes else None,
        "approx": low_approx,   # low 補值 → KD/ATR 為近似,前端標「≈」
    }

    # ── 健康分(呼叫端未給時,盤後場景由 dec_health 取) ──
    hs = None
    if health:
        hs = health.get("health_score") or health.get("score")
    if hs is None:
        try:
            import db
            with db._lock, db._conn() as c:
                r = c.execute("""SELECT score, grade FROM dec_health
                    WHERE code=? ORDER BY trade_date DESC LIMIT 1""",
                    (code,)).fetchone()
                if r:
                    hs = r["score"]
                    grade = grade or r["grade"]
        except Exception:
            pass

    # ── 交易計畫(ATR 結構;引擎股不給計畫) ────────────
    trade_block = {"advice": None, "buy": None, "stop": None,
                   "t1": None, "t2": None, "rr": None}
    is_engine = code in getattr(C, "ENGINE_STOCKS", set())
    atr_v = tech_block["atr"]
    ref_high = (snap or {}).get("high") or (highs[-1] if highs else None)
    if is_engine and closes:
        # v3.0 引擎軌(波段):收盤進場、月線停損、目標放寬
        ma20 = I.sma(closes, 20)
        buy = round(closes[-1], 1)
        if grade and ma20 and atr_v:
            stop = round(ma20, 1)
            t1 = round(buy + 3 * atr_v, 1)
            t2 = round(buy + 6 * atr_v, 1)
            rr = round((t1 - buy) / (buy - stop), 2) if buy > stop else None
            trade_block.update({
                "advice": {"Ready": "波段進場(站上月線)", "Watch": "等站回月線",
                           "Hold": "觀望"}.get(grade, "等待"),
                "buy": buy, "stop": stop, "t1": t1, "t2": t2, "rr": rr})
        else:
            trade_block["advice"] = "引擎軌:等站回月線"
    elif grade and atr_v and ref_high:
        buy = round(ref_high, 1)
        stop = round(buy - STOP_ATR * atr_v, 1)
        t1 = round(buy + T1_ATR * atr_v, 1)
        t2 = round(buy + T2_ATR * atr_v, 1)
        rr = round((t1 - buy) / (buy - stop), 2) if buy > stop else None
        trade_block.update({
            "advice": {"Ready": "突破進場(攻擊軌)", "Watch": "等待",
                       "Hold": "觀望"}.get(grade, "等待"),
            "buy": buy, "stop": stop, "t1": t1, "t2": t2, "rr": rr})
    elif grade:
        trade_block["advice"] = {"Ready": "突破進場", "Watch": "等待",
                                 "Hold": "觀望"}.get(grade, "等待")

    # ── AI 結論(✓/✕ 原因,全部來自真實欄位) ──────────
    reasons = []
    def mark(ok, txt_ok, txt_no):
        reasons.append(("✓ " + txt_ok) if ok else ("✕ " + txt_no))
    if chip_block["big400_delta"] is not None:
        mark(chip_block["big400_delta"] > 0, "大戶增加", "大戶未增")
    if flow_block["active_buy_pct"] is not None:
        mark(flow_block["active_buy_pct"] > 50, "主動資金翻正", "主動賣壓偏重")
    ma_up = [tech_block[k] for k in ("ma5", "ma10", "ma20")]
    if any(v is not None for v in ma_up):
        mark(all(v == "↑" for v in ma_up if v is not None),
             "技術多頭", "均線未全數翻多")
    if chip_block["foreign"] is not None:
        mark((chip_block["foreign"] or 0) > 0, "外資買超", "外資未進")
    if chip_block["main_force"] is None:
        reasons.append("✕ 籌碼尚未完全集中(分點資料待接)")
    ai_pct = hs

    return {
        "code": code, "name": name, "sector": sector,
        "stock_type": "engine" if is_engine else styp,
        "price": (snap or {}).get("price") or (closes[-1] if closes else None),
        "change_rate": (snap or {}).get("change_rate"),
        "health_score": hs, "grade": grade,
        "chip": chip_block, "flow": flow_block, "tech": tech_block,
        "trade": trade_block,
        "ai": {"pct": ai_pct, "reasons": reasons},
        "generated": datetime.now(TW_TZ).isoformat(timespec="seconds"),
    }


# ════════════════════════════════════════════════════════
# 盤面速覽:資金流入前三族群(當日成交金額,億)
# ════════════════════════════════════════════════════════
def market_brief(snaps=None):
    """
    回傳 [{sector, amount_yi, dir}] 前三(僅攻擊族群)。
    amount = 族群成員 total_amount 加總;dir 用 sector_daily flow_dir(有存才給)。
    """
    if snaps is None:
        try:
            import eod_pipeline
            snaps = eod_pipeline.fetch_eod_snaps()
        except Exception as e:
            print(f"[stock_card] 快照取得失敗:{e}")
            return []
    agg = {}
    for s in snaps:
        code = s.get("code")
        sec, styp = C.SECTOR_MAP.get(code, (None, None))
        if not sec or code in getattr(C, "ENGINE_STOCKS", set()):
            pass
        if not sec or styp != "attack":
            continue
        agg[sec] = agg.get(sec, 0) + (s.get("total_amount") or 0)
    dirs = {}
    try:
        import db
        with db._lock, db._conn() as c:
            for r in c.execute("""SELECT sector, flow_dir FROM sector_daily
                WHERE trade_date=(SELECT MAX(trade_date) FROM sector_daily)"""):
                dirs[r["sector"]] = r["flow_dir"]
    except Exception:
        pass
    top = sorted(agg.items(), key=lambda kv: -kv[1])[:3]
    return [{"sector": k, "amount_yi": round(v / 1e8, 1),
             "dir": ("↑" if dirs.get(k, 1) > 0 else "↓") if k in dirs else None}
            for k, v in top]


# 冒煙測試:python stock_card.py
if __name__ == "__main__":
    import random
    random.seed(5)
    bars, p = [], 100.0
    for i in range(70):
        p *= 1 + random.uniform(-0.015, 0.02)
        bars.append({"date": f"2026-06-{(i % 28) + 1:02d}", "close": round(p, 1),
                     "high": round(p * 1.015, 1), "low": round(p * 0.985, 1),
                     "volume": random.randint(2000, 9000)})
    snap = {"code": "2383", "price": bars[-1]["close"],
            "high": bars[-1]["high"], "change_rate": 2.1,
            "buy_volume": 6200, "sell_volume": 3800}
    card = build_card("2383", snap=snap, grade="Ready",
                      injected_bars=bars,
                      chip_detail={"foreign_net_d": 3582, "trust_net_d": 1240,
                                   "dealer_net_d": -325, "foreign_net_20d": 15000,
                                   "big400_pct": 62.1, "big400_delta": 2.3,
                                   "big1000_pct": 41.5, "big1000_delta": 0.8,
                                   "main_force_net": None})
    import json
    print(json.dumps(card, ensure_ascii=False, indent=1))
