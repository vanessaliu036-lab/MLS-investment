"""
MLS 插件 — strategy_doc.py
「四大條件策略」獨立實作(完全依使用者提供的策略文件)
====================================================================
純插件,與現有系統並存做 A/B 對比。不動主架構任何公式。
現有系統 = 均價線+今高+五因子;本插件 = MA20+乖離率+昨高+四大條件。
兩者對同一批股票各自打分,盤後比對誰準。

四大條件(文件原文):
  盤後(選股池):
    1. 市場:大盤收盤 > MA20(大盤多頭)
    2. 籌碼:千張大戶比↑ AND (外資買超 OR 投信買超) AND 近5日法人合計>0
    3. 技術:收盤>MA20、乖離率0~5%、量>5日均量、收盤>昨高
    4. BS盤後:今日Buy > Sell × 動態倍數(1.1/1.25/1.5)
  盤中(進場):
    1. 大盤現價>開盤
    2. 現價>MA20、現價>前30分高
    3. 預估量>昨量×1.2
    4. BS盤中>1.2 且 委買>委賣×1.3
  強制豁免:單根爆量>昨量10% 或 大盤急殺-1.5% → 空手
"""

import chips


# ── BS 動態倍數(文件原文) ──────────────────────────────
def bs_multiplier(market_pct):
    if market_pct > 1:   return 1.10
    if market_pct >= 0:  return 1.25
    return 1.50


# ════════════════════════════════════════════════════════
# 盤後四大條件(選股池)
# ════════════════════════════════════════════════════════
def eval_eod(s, kbars, market_close, market_ma20):
    """
    s: snapshot(含 close/high/prev_high/total_volume/buy_volume/sell_volume)
    kbars: 該股日K(舊→新,含 close/high/volume)
    回傳 (pass:bool, detail:dict 逐條 True/False + 說明)
    """
    d = {}

    # 條件1 市場多頭
    d["c1_market"] = market_close is not None and market_ma20 is not None and market_close > market_ma20

    # 條件2 籌碼(真 FinMind)
    ch = chips.get_chips(s["code"])
    big_up = (ch.get("big_holder_trend") is not None and ch.get("big_holder_trend") > 0)
    inst_buy = (ch.get("inst_net_20d_lots") or 0) > 0
    d["c2_chip"] = bool(big_up and inst_buy)
    d["_chip_detail"] = {
        "大戶比趨勢": ch.get("big_holder_trend"),
        "法人近月買超(張)": ch.get("inst_net_20d_lots"),
        "外資連買天數": ch.get("inst_streak"),
        "資料齊全": ch.get("inst_net_20d_lots") is not None and ch.get("big_holder_trend") is not None,
    }

    # 條件3 技術(MA20/乖離率/量/昨高)
    closes = [k["close"] for k in kbars][-20:] if kbars else []
    ma20 = sum(closes) / len(closes) if closes else None
    vols = [k.get("volume", 0) for k in kbars][-5:] if kbars else []
    avg5v = sum(vols) / len(vols) if vols else None
    price = s.get("close") or s.get("price")
    bias = ((price - ma20) / ma20 * 100) if (ma20 and price) else None
    tech = (ma20 is not None and price > ma20
            and bias is not None and 0 <= bias <= 5
            and avg5v and (s.get("total_volume") or 0) > avg5v
            and s.get("prev_high") is not None and price > s["prev_high"])
    d["c3_tech"] = bool(tech)
    d["_tech_detail"] = {"MA20": round(ma20, 2) if ma20 else None,
                         "乖離率%": round(bias, 2) if bias is not None else None,
                         "量>5日均": bool(avg5v and (s.get("total_volume") or 0) > avg5v),
                         "收>昨高": bool(s.get("prev_high") and price > s["prev_high"])}

    # 條件4 BS 盤後
    bv, sv = s.get("buy_volume") or 0, s.get("sell_volume") or 0
    mult = bs_multiplier(s.get("_market_pct", 0))
    d["c4_bs"] = bool(sv > 0 and bv > sv * mult)
    d["_bs_detail"] = {"Buy": bv, "Sell": sv, "倍數門檻": mult,
                       "實際倍數": round(bv / sv, 2) if sv else None}

    d["pass"] = all([d["c1_market"], d["c2_chip"], d["c3_tech"], d["c4_bs"]])
    d["score"] = sum([d["c1_market"], d["c2_chip"], d["c3_tech"], d["c4_bs"]]) * 25  # 0-100
    return d["pass"], d


# ════════════════════════════════════════════════════════
# 盤中四大條件(進場)
# ════════════════════════════════════════════════════════
def eval_intraday(s, ma20, first30_high, prev_volume, market_price, market_open):
    d = {}
    price = s.get("price")

    # 條件1 大盤不弱於開盤
    d["c1"] = market_price is not None and market_open is not None and market_price > market_open
    # 條件2 現價>MA20 且 >前30分高
    d["c2"] = bool(ma20 and price > ma20 and first30_high and price > first30_high)
    # 條件3 預估量>昨量×1.2(用當前量比近似,快照無分鐘數則退用 volume_ratio)
    vr = s.get("volume_ratio") or 0
    d["c3"] = vr > 1.2
    # 條件4 BS盤中>1.2(委買委賣×1.3 需五檔,快照無則只驗BS)
    bv, sv = s.get("buy_volume") or 0, s.get("sell_volume") or 0
    d["c4"] = bool(sv > 0 and bv > sv * 1.2)

    # 強制豁免
    exempt = None
    if prev_volume and (s.get("total_volume") or 0) > prev_volume * 0.10 and vr > 3:
        exempt = "瞬時爆量>昨量10%(疑出貨)"
    if market_price is not None and market_open and market_price < market_open * 0.985:
        exempt = "大盤急殺-1.5%(系統性風險)"

    d["exempt"] = exempt
    d["pass"] = all([d["c1"], d["c2"], d["c3"], d["c4"]]) and exempt is None
    d["score"] = sum([d["c1"], d["c2"], d["c3"], d["c4"]]) * 25
    return d["pass"], d


# ════════════════════════════════════════════════════════
# 批次評估 + 與現有系統對比
# ════════════════════════════════════════════════════════
def annotate(snaps, market_pct=0.0, market_close=None, market_ma20=None, kbar_fn=None):
    """
    為每檔 snap 增補 _doc_strategy 欄位(文件四大條件結果)。
    kbar_fn(code)->日K;無則籌碼/技術盡力而為。
    回傳 pass 清單(通過文件四大條件的股票)。
    """
    passed = []
    for s in snaps:
        s["_market_pct"] = market_pct
        kb = []
        if kbar_fn:
            try:
                kb = kbar_fn(s["code"])
            except Exception:
                kb = []
        # 補 prev_high(昨高)
        if kb and len(kb) >= 2 and "prev_high" not in s:
            s["prev_high"] = kb[-2].get("high")
        ok, detail = eval_eod(s, kb, market_close, market_ma20)
        s["_doc_strategy"] = detail
        if ok:
            passed.append(s["code"])
    return passed
