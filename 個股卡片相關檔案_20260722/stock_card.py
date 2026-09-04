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
import os
import urllib.parse
import urllib.request
import json

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
        out.append({"date": str(r.get("date") or r.get("ts") or r.get("index"))[:10], "close": cl,
                    "high": r.get("high"), "low": r.get("low"),
                    "volume": r.get("volume", 0), "amount": r.get("amount")})
    return out


_MARKET_VWAP_CACHE = {}


def _market_vwap_finmind(code, asof=None, days=20):
    """用 FinMind 官方日行情的成交金額／成交量計算市場 20 日 VWAP。

    TaiwanStockPrice 的 Trading_money 是元、Trading_Volume 是股；
    不用收盤價、不用法人買賣超、不用 Shioaji 的張數欄位代替。
    """
    limit = str(asof or datetime.now(TW_TZ).date())[:10]
    key = (str(code), limit, int(days))
    if key in _MARKET_VWAP_CACHE:
        return _MARKET_VWAP_CACHE[key]
    try:
        end = datetime.strptime(limit, "%Y-%m-%d").date()
        start = (end - timedelta(days=90)).isoformat()
        query = urllib.parse.urlencode({
            "dataset": "TaiwanStockPrice", "data_id": str(code),
            "start_date": start, "end_date": limit,
        })
        token = os.environ.get("FINMIND_TOKEN", "").strip()
        if token:
            query += "&" + urllib.parse.urlencode({"token": token})
        req = urllib.request.Request(
            "https://api.finmindtrade.com/api/v4/data?" + query,
            headers={"User-Agent": "MLS/4 market-vwap"},
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            rows = json.loads(response.read().decode("utf-8")).get("data") or []
        usable = []
        for row in rows:
            try:
                date = str(row.get("date") or "")[:10]
                amount = float(row.get("Trading_money"))
                volume = float(row.get("Trading_Volume"))
            except (TypeError, ValueError):
                continue
            if date <= limit and amount > 0 and volume > 0:
                usable.append((date, amount, volume))
        usable.sort(key=lambda item: item[0])
        if len(usable) < days:
            result = (None, len(usable), None, None)
        else:
            window = usable[-days:]
            total_amount = sum(item[1] for item in window)
            total_volume = sum(item[2] for item in window)
            result = (round(total_amount / total_volume, 2), len(window),
                      window[0][0], window[-1][0]) if total_volume else (None, 0, None, None)
    except Exception as exc:
        print(f"[stock_card] FinMind 市場成交均價 {code} 失敗: {exc}", flush=True)
        result = (None, 0, None, None)
    _MARKET_VWAP_CACHE[key] = result
    return result


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
               injected_bars=None, chip_detail=None, chip_asof=None):
    """
    組完整資訊卡。snap(盤中/收盤快照)、health(money_health 或
    dec_health 結果)、grade(Ready/Watch/Hold)由呼叫端提供可省 API;
    缺省時自行降級取得。
    """
    name = C.NAME_MAP.get(code, code)
    sector, styp = C.SECTOR_MAP.get(code, ("其他", "attack"))
    bars = _bars(code, injected=injected_bars)
    (market_vwap_20d, market_vwap_days, market_vwap_start,
     market_vwap_end) = _market_vwap_finmind(code, chip_asof, 20)
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
            chip_detail = chips.get_chips_detail(code, asof=chip_asof)
        except Exception as e:
            print(f"[stock_card] 籌碼細項失敗:{e}", flush=True)
            chip_detail = {}
    cd = chip_detail or {}
    # 法人占成交量比:用法人當日淨買賣超(張)佔當日成交量(張)的比重,
    # 同樣 1,000 張買超,量 5,000 張跟量 100,000 張意義完全不同——這是量比後
    # 才看得出來的「力度」,不是另一個籌碼分數,純比例換算,查無成交量給 None。
    today_vol_lots = None
    if bars and bars[-1].get("date") == cd.get("source_date"):
        v = bars[-1].get("volume")
        if v:
            # broker.daily_kbars 與 extras._authoritative_daily_bars 的 volume
            # 邊界都已統一為「張」；不能再除以 1,000，否則法人佔量比會
            # 被放大 1,000 倍。FinMind 只有在進入 adapter 時才以股表示。
            today_vol_lots = v
    def _pct_of_volume(net_lots):
        if net_lots is None or not today_vol_lots:
            return None
        return round(net_lots / today_vol_lots * 100, 2)
    chip_block = {
        "foreign": cd.get("foreign_net_d"), "trust": cd.get("trust_net_d"),
        "dealer": cd.get("dealer_net_d"),
        "dealer_self": cd.get("dealer_self_d"),
        "dealer_hedge": cd.get("dealer_hedge_d"),
        "foreign_net_3d": cd.get("foreign_net_3d"),
        "trust_net_3d": cd.get("trust_net_3d"),
        "inst_net_3d_lots": cd.get("inst_net_3d_lots"),
        "foreign_net_5d": cd.get("foreign_net_5d"),
        "trust_net_5d": cd.get("trust_net_5d"),
        "dealer_net_5d": cd.get("dealer_net_5d"),
        "inst_net_5d_lots": cd.get("inst_net_5d_lots"),
        "inst_streak": cd.get("inst_streak"),
        "trust_streak": cd.get("trust_streak"),
        "inst_net_20d_lots": cd.get("inst_net_20d_lots"),
        "market_vwap_20d": market_vwap_20d,
        "market_vwap_20d_days": market_vwap_days,
        "market_vwap_20d_start": market_vwap_start,
        "market_vwap_20d_end": market_vwap_end,
        "market_vwap_20d_source": "FinMind TaiwanStockPrice：Trading_money ÷ Trading_Volume",
        "foreign_net_20d": cd.get("foreign_net_20d"),
        "trust_net_20d": cd.get("trust_net_20d"),
        "dealer_net_20d": cd.get("dealer_net_20d"),
        "main_force": cd.get("main_force_net"),     # premium 才有,現為 None
        "big400_delta": cd.get("big400_delta"),
        "big1000_delta": cd.get("big1000_delta"),
        "big_holder_delta": cd.get("big400_delta"),  # 卡片「大戶持股」= 400張級距變化
        "margin_change_d": cd.get("margin_change_d"),
        "margin_change_5d": cd.get("margin_change_5d"),
        "margin_balance": cd.get("margin_balance"),
        "margin_source_date": cd.get("margin_source_date"),
        "short_balance": cd.get("short_balance"),
        "short_change_d": cd.get("short_change_d"),
        "short_change_5d": cd.get("short_change_5d"),
        "short_margin_ratio": cd.get("short_margin_ratio"),
        "lending_volume_d": cd.get("lending_volume_d"),
        "lending_balance": cd.get("lending_balance"),
        "lending_balance_change_d": cd.get("lending_balance_change_d"),
        "lending_source_date": cd.get("lending_source_date"),
        "foreign_share_pct": cd.get("foreign_share_pct"),
        "foreign_share_change": cd.get("foreign_share_change"),
        "foreign_share_remain_pct": cd.get("foreign_share_remain_pct"),
        "foreign_share_source_date": cd.get("foreign_share_source_date"),
        "foreign_pct_volume": _pct_of_volume(cd.get("foreign_net_d")),
        "trust_pct_volume": _pct_of_volume(cd.get("trust_net_d")),
        "inst_pct_volume": _pct_of_volume(
            None if cd.get("foreign_net_d") is None or cd.get("trust_net_d") is None
            or cd.get("dealer_net_d") is None else
            cd["foreign_net_d"] + cd["trust_net_d"] + cd["dealer_net_d"]),
        "today_volume_lots": round(today_vol_lots) if today_vol_lots else None,
        "source": cd.get("source") or "FinMind 盤後法人",
        "source_url": cd.get("source_url"),
        "source_date": cd.get("source_date"),
        "chip_data_date": cd.get("source_date"),
        "chip_source_table": "chips_cache.json",
        "chip_source_version": cd.get("schema_version") or "chip_ssot_v1",
        "sources": cd.get("sources"),
        "period_note": "法人=T-1 盤後蓋章(非即時);大戶級距=集保週資料;主力分點=待接籌碼商;"
                       "外資持股比=集保週資料,更新日獨立標示,不跟日資料比對新鮮度",
    }

    # ── 資金 ────────────────────────────────────────────
    bv = (snap or {}).get("buy_volume") or 0
    sv = (snap or {}).get("sell_volume") or 0
    tot = bv + sv
    flow_block = {
        "net_active": (snap or {}).get("aflow"),
        "net_active_source": "intraday_eod.db 盤後蓋章" if (snap or {}).get("aflow") is not None else None,
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
        close = (snap or {}).get("price")
        change = (snap or {}).get("change_rate")
        breakout_already_happened = (
            close is not None and float(close) >= buy
            and change is not None and float(change) >= 9.0
        )
        trade_block.update({
            "advice": ("觀察突破後承接" if breakout_already_happened else
                       {"Ready": "突破進場(攻擊軌)", "Watch": "等待",
                        "Hold": "觀望"}.get(grade, "等待")),
            # 已收盤突破的股票不能再把當日高點顯示成「尚未觸發」買點。
            "buy": None if breakout_already_happened else buy,
            "reference_high": buy,
            "breakout_status": ("已突破，觀察是否守住前高與隔日承接"
                                 if breakout_already_happened else "尚未確認"),
            "stop": stop, "t1": t1, "t2": t2, "rr": rr})
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

    # chip_quality：法人近月淨額/連買天數一律以本卡新鮮 chip_block（FinMind/官方
    # 快取,source_date=最新交易日）為準,不採 dec_health.chip_note——後者由 mls-v4
    # 已凍結的 inst_daily 產生,會回過期連買天數（南亞科案:真連買7日卻顯示連買4日）。
    # 對齊「同一事實只准一套算法,後台算事實前台只翻譯」。chip_block 無資料才回退。
    _streak = chip_block.get("inst_streak")
    _net20 = chip_block.get("inst_net_20d_lots")
    _cq_parts = []
    if _net20 is not None:
        _cq_parts.append(f"法人近月{_net20:+,}張")
    if _streak:
        _cq_parts.append(f"連{'買' if _streak > 0 else '賣'}{abs(int(_streak))}日")
    chip_quality = ",".join(_cq_parts) if _cq_parts else (
        (health or {}).get("chip_quality") if health else None)

    return {
        "code": code, "name": name, "sector": sector,
        "stock_type": "engine" if is_engine else styp,
        "price": (snap or {}).get("price") or (closes[-1] if closes else None),
        "change_rate": (snap or {}).get("change_rate"),
        "health_score": hs, "grade": grade,
        "health_module_scores": (health or {}).get("module_scores") if health else None,
        "health_quadrant": (health or {}).get("quadrant") if health else None,
        "health_label": (health or {}).get("label") if health else None,
        "health_stars": (health or {}).get("stars") if health else None,
        "chip_quality": chip_quality,
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
