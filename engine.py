"""
MLS 標準版 — engine.py
核心決策流程,嚴格對應使用者定義的漏斗:

  ① 資金流入板塊計算  → 全族群資金流入分數排行
  ② 取前三族群        → TOP_SECTORS=3
  ③ 每族群選龍頭      → 共 3 檔龍頭個股
  ④ 龍頭深度分析      → 法人近月買賣超 / 大戶比例 / 均線上方空間 / AI建議
  ⑤ 其餘候選股        → 規則引擎照常產進場/觀察/出場訊號(熱力表)
"""

from statistics import median
from datetime import datetime, timezone, timedelta

import config as C
import broker
import chips
import scoring

TW_TZ = timezone(timedelta(hours=8))

_avg5v = {}          # code → 5日均量(股),盤前/首次懶載入
_ENTRY_MIN = {"v": None}


def _entry_min():
    """自適應進場門檻(80%準度控制器,盤後由 after_hours 調整)。"""
    if _ENTRY_MIN["v"] is None:
        try:
            import db as _db
            _ENTRY_MIN["v"] = float(_db.kv_get("entry_score_min", 40))
        except Exception:
            _ENTRY_MIN["v"] = 40.0
    return _ENTRY_MIN["v"]


def reload_entry_min():
    _ENTRY_MIN["v"] = None


def _avg5_volume(code):
    if code in _avg5v:
        return _avg5v[code]
    kb = broker.daily_kbars(code, days=8)
    v = None
    if kb and "volume" in kb[0]:
        vols = [k["volume"] for k in kb[-5:] if k.get("volume")]
        v = sum(vols) / len(vols) if vols else None
    _avg5v[code] = v
    return v

# 前一輪族群成交金額佔比(算流入增幅用;跨輪保存在模組層)
_prev_amount_share = {}


# ══════════════════════════════════════════════════════════
# ① 資金流入板塊計算
# ══════════════════════════════════════════════════════════
def compute_sector_flow(snaps):
    """
    輸入全市場快照,輸出族群列表(含 flow_score 資金流入分數)排行。
    flow_score = 成交金額佔比增幅(相對上一輪) * FLOW_W_AMOUNT
               + 中位漲幅 * FLOW_W_CHANGE
    """
    global _prev_amount_share
    total_amt = sum(s["total_amount"] for s in snaps) or 1

    groups = {}
    for s in snaps:
        sec, stype = C.SECTOR_MAP.get(s["code"], (None, None))
        if not sec:
            continue
        s["sector"], s["sector_type"] = sec, stype
        groups.setdefault(sec, {"type": stype, "members": []})["members"].append(s)

    sectors = []
    for name, g in groups.items():
        ms = g["members"]
        med = median([m["change_rate"] for m in ms])
        amt = sum(m["total_amount"] for m in ms)
        share = amt / total_amt * 100                       # 佔全市場 %
        share_delta = share - _prev_amount_share.get(name, share)
        flow_score = share_delta * 100 * C.FLOW_W_AMOUNT + med * C.FLOW_W_CHANGE * 10
        _prev_amount_share[name] = share

        leader_raw = max(ms, key=lambda m: m["change_rate"])
        locked = (
            g["type"] == "attack"
            and med > C.L1_SECTOR_MEDIAN
            and leader_raw["change_rate"] > C.L1_LEADER
        )
        sectors.append({
            "name": name, "type": g["type"],
            "pct": round(med, 2),
            "amount_100m": round(amt / 1e8, 1),
            "amount_share": round(share, 2),
            "flow_score": round(flow_score, 1),
            "locked": locked,
            "members": ms,
        })

    sectors.sort(key=lambda x: x["flow_score"], reverse=True)
    for i, s in enumerate(sectors):
        s["rank"] = i + 1
    return sectors


# ══════════════════════════════════════════════════════════
# ②③ 前三族群 → 每族群龍頭
# ══════════════════════════════════════════════════════════
def pick_leaders(sectors):
    """
    取資金流入前 TOP_SECTORS 族群,每族群依龍頭分數選 1 檔。
    龍頭分數 = amount_rank*0.5 + change_rank*0.3 + vr_rank*0.2(族群內正規化)
    """
    top = sectors[:C.TOP_SECTORS]
    leaders = []
    eng = getattr(C, "ENGINE_STOCKS", set())
    for sec in top:
        ms = [m for m in sec["members"] if m["code"] not in eng]  # 引擎成員永不為龍頭
        if not ms:
            continue
        def rank_norm(key):
            order = sorted(ms, key=lambda m: m[key], reverse=True)
            return {m["code"]: 1 - i / max(1, len(order) - 1) if len(order) > 1 else 1
                    for i, m in enumerate(order)}
        ra = rank_norm("total_amount")
        rc = rank_norm("change_rate")
        rv = rank_norm("volume_ratio")
        best = max(ms, key=lambda m:
                   ra[m["code"]] * C.LEADER_W["amount"]
                   + rc[m["code"]] * C.LEADER_W["change"]
                   + rv[m["code"]] * C.LEADER_W["vr"])
        leaders.append({"sector": sec["name"], "sector_rank": sec["rank"],
                        "sector_type": sec["type"], "snap": best})
    return leaders


# ══════════════════════════════════════════════════════════
# ④ 龍頭深度分析:法人近月 / 大戶 / 均線上方空間 / AI建議
# ══════════════════════════════════════════════════════════
def analyze_leader(leader):
    s = leader["snap"]
    code = s["code"]

    # 均線與前高(Shioaji 日K)
    ma_val, ma_bias, high_space = None, None, None
    kb = broker.daily_kbars(code, days=C.HIGH_LOOKBACK_DAYS + 5)
    if kb:
        closes = [k["close"] for k in kb]
        highs = [k["high"] for k in kb]
        if len(closes) >= C.MA_SPACE_MA:
            ma_val = round(sum(closes[-C.MA_SPACE_MA:]) / C.MA_SPACE_MA, 2)
            if s["price"] and ma_val:
                ma_bias = round((s["price"] - ma_val) / ma_val * 100, 2)  # 乖離%
        hh = max(highs[-C.HIGH_LOOKBACK_DAYS:]) if highs else None
        if hh and s["price"]:
            high_space = round((hh - s["price"]) / s["price"] * 100, 2)   # 距前高%

    # 籌碼(FinMind 盤後,日快取)
    ch = chips.get_chips(code)

    # AI 建議(規則模板,盤中不呼叫 LLM — 依 MLS 定案)
    advice, stance = _ai_advice(leader, ma_bias, high_space, ch)

    return {
        "code": code,
        "name": C.NAME_MAP.get(code, code),
        "sector": leader["sector"],
        "sector_rank": leader["sector_rank"],
        "sector_type": leader["sector_type"],
        "price": s["price"], "change_rate": round(s["change_rate"], 2),
        "volume_ratio": round(s["volume_ratio"], 2),
        f"ma{C.MA_SPACE_MA}": ma_val,
        "ma_bias_pct": ma_bias,          # 均線上方空間(乖離,+為在均線上方)
        "high_space_pct": high_space,    # 距60日前高空間(壓力上方空間)
        "inst_net_20d_lots": ch["inst_net_20d_lots"],
        "inst_streak": ch["inst_streak"],
        "big_holder_pct": ch["big_holder_pct"],
        "big_holder_trend": ch["big_holder_trend"],
        "ai_stance": stance,             # bullish / neutral / caution
        "ai_advice": advice,
    }


def _ai_advice(leader, ma_bias, high_space, ch):
    """
    規則模板生成建議(2~3句)。主引擎鐵則:只給環境判讀,絕不給進場語。
    """
    s = leader["snap"]
    pts, score = [], 0

    inst = ch["inst_net_20d_lots"]
    if inst is not None:
        if inst > 0:
            pts.append(f"法人近月買超 {inst:,} 張"); score += 2
        else:
            pts.append(f"法人近月賣超 {abs(inst):,} 張"); score -= 2
    if ch["inst_streak"] and ch["inst_streak"] >= 3:
        pts.append(f"外資連買 {ch['inst_streak']} 日"); score += 1
    if ch["inst_streak"] and ch["inst_streak"] <= -3:
        pts.append(f"外資連賣 {abs(ch['inst_streak'])} 日"); score -= 1

    if ch["big_holder_pct"] is not None:
        t = ch["big_holder_trend"]
        if t is not None and t > 0.3:
            pts.append(f"千張大戶 {ch['big_holder_pct']}% 且近月增 {t}pp"); score += 1
        elif t is not None and t < -0.3:
            pts.append(f"千張大戶 {ch['big_holder_pct']}% 但近月減 {abs(t)}pp"); score -= 1
        else:
            pts.append(f"千張大戶持股 {ch['big_holder_pct']}%")

    if ma_bias is not None:
        if 0 <= ma_bias <= 8:
            pts.append(f"站上 MA{C.MA_SPACE_MA} 乖離 {ma_bias}% 未過熱"); score += 1
        elif ma_bias > 8:
            pts.append(f"MA{C.MA_SPACE_MA} 乖離達 {ma_bias}%,短線過熱"); score -= 1
        else:
            pts.append(f"仍在 MA{C.MA_SPACE_MA} 之下 {abs(ma_bias)}%"); score -= 1

    if high_space is not None:
        if high_space > 5:
            pts.append(f"距前高尚有 {high_space}% 空間"); score += 1
        elif high_space >= 0:
            pts.append(f"逼近前高(剩 {high_space}%),留意解套賣壓")
        else:
            pts.append("已創60日新高,上方無套牢壓力"); score += 1

    if leader["sector_type"] == "engine":
        stance = "neutral"
        advice = "主引擎族群,僅作市場溫度計:" + ";".join(pts[:3]) + \
                 "。依鐵則不列入進場,外資動向轉賣時視為環境轉差警訊。"
        return advice, stance

    if score >= 3:
        stance = "bullish"
        advice = ";".join(pts[:4]) + "。籌碼與位階俱佳,符合攻擊部隊快打條件,依 ABAB 節奏操作、破均線即出。"
    elif score >= 0:
        stance = "neutral"
        advice = ";".join(pts[:4]) + "。條件混合,建議等量能或法人方向確認再動作,嚴設停損。"
    else:
        stance = "caution"
        advice = ";".join(pts[:4]) + "。籌碼面轉弱,攻擊部隊鐵則:不戀戰,反彈視為減碼機會。"
    return advice, stance


# ══════════════════════════════════════════════════════════
# ⑤ 規則引擎(其餘候選 → 熱力表)
# ══════════════════════════════════════════════════════════
def eval_stock(s, locked_sectors, *, sector_median=0.0, market_pct=0.0,
               abab_a_day=False, mode="attack", is_leader=False):
    rules, price, avgp = [], s["price"] or 0, s.get("avg_price") or 0
    if price and s["high"] and price >= s["high"] and s["change_rate"] > 0:
        rules.append("突破今高")
    if s["volume_ratio"] >= C.R005_VR:
        rules.append("爆量")
    if "突破今高" in rules and s["volume_ratio"] >= C.R006_VR:
        rules.append("帶量突破")
    if avgp and price >= avgp and s["change_rate"] > 0:
        rules.append("站上均價線")

    # ── 五因子評分(TNVR/aflow/RS/籌碼/懲罰) ──
    aflow = scoring.update_aflow(s["code"], s.get("total_volume"),
                                 s.get("tick_type"))
    tnvr_val = scoring.tnvr(s.get("total_volume"), _avg5_volume(s["code"]))
    chip = chips.get_chips(s["code"])
    in_locked = s.get("sector") in locked_sectors and s.get("sector_type") == "attack"
    score, factors, penalties, div_flag = scoring.score_stock(
        s, sector_median=sector_median, market_pct=market_pct,
        locked=in_locked, abab_a_day=abab_a_day, chip=chip,
        tnvr_val=tnvr_val, aflow_val=aflow, mode=mode)

    risk = []
    caution = []  # 注意但不至於直接 sell(大跌盤普跌,避免誤觸)
    if avgp and price < avgp and s["change_rate"] < 0:
        caution.append("跌破均價線")
    if s.get("sector_type") == "attack" and s.get("sector") not in locked_sectors \
            and s["change_rate"] < -2:
        caution.append("族群轉弱")
    # 強烈風險:假紅背離(出貨鐵律,個股層級)
    strong_risk = []
    if div_flag == "fake_red":
        strong_risk.append("假紅背離")
    # 強烈風險:大跌(>5%) + 量縮(量比<0.8)+ 跌破均價 → 真的弱勢,出場
    if s["change_rate"] < -5 and (s.get("volume_ratio") or 0) < 0.8 \
            and avgp and price < avgp:
        strong_risk.append("弱勢放量")

    entry_n = len(rules)
    is_engine_stock = s["code"] in getattr(C, "ENGINE_STOCKS", set())

    # 龍頭特例:法人買超/外資連買是「進場理由」,跌破均價應是 watch(等回檔)
    # 而不是 sell。只有當法人/外資翻負才走正常 sell 流程。
    leader_chip_pos = (chip and
                       ((chip.get("inst_net_20d_lots") or 0) > 0
                        or (chip.get("inst_streak") or 0) >= 3))

    if strong_risk and not (is_leader and leader_chip_pos):
        action, ec, rules = "sell", "risk", strong_risk
    elif s.get("sector_type") == "engine" or is_engine_stock:
        action, ec = "obs", "monitor"          # 主引擎/引擎成員鐵則:只觀察
    elif div_flag == "pull_sell":
        action, ec = "watch", "potential"      # 邊拉邊賣:降級觀察,不給進場
        rules = rules + ["邊拉邊賣⚠"]
    elif is_leader and leader_chip_pos:
        # 龍頭 + 法人未斷 → 即使大跌盤也標 watch(等回檔),不賣
        action, ec = "watch", "potential"
        rules = rules + (caution if caution else ["龍頭回檔觀察"])
    elif entry_n >= 2 and score >= _entry_min() + 15:
        action, ec = "buy", "entry_high"
    elif entry_n >= 1 and score >= _entry_min():
        action, ec = "buy", "entry"
    elif caution or s["change_rate"] >= C.LONE_WOLF_PCT:
        # 注意訊號(普跌盤跌破均價/族群轉弱)→ 觀察而非直接 sell
        action, ec = "watch", "potential"
        rules = rules + (caution if caution else ["孤狼訊號"])
    else:
        action, ec = "obs", "monitor"

    conf = entry_n + (1 if in_locked else 0)
    # 動態停損:均價線緩衝 與 今日低點 取高(洗盤日不被掃太遠)
    stop = None
    if avgp or s.get("low"):
        stop = round(max(avgp * 0.985 if avgp else 0, s.get("low") or 0), 2)
    return {
        "action": action,
        "ai_score": score,
        "factors": factors, "penalties": penalties,
        "tnvr": tnvr_val,
        "code": s["code"], "name": C.NAME_MAP.get(s["code"], s["code"]),
        "sector": s.get("sector", "其他"), "sector_type": s.get("sector_type", "attack"),
        "price": s["price"], "change_rate": round(s["change_rate"], 2),
        "volume_ratio": round(s["volume_ratio"], 2),
        "avg_price": s.get("avg_price"),
        "suggested_stop": stop,
        "confidence_label": "high" if conf >= 3 else ("mid" if conf >= 2 else "low"),
        "rules": rules + [p.split(":")[0] for p in penalties],
        "heat_level": {"entry_high": "strong", "entry": "hot", "potential": "warm",
                       "monitor": "neutral", "risk": "cold"}[ec],
    }


# ══════════════════════════════════════════════════════════
# 主流程:一次掃描 → 完整狀態
# ══════════════════════════════════════════════════════════
def build_state(watchlist_codes=None):
    """watchlist_codes: 今日觀察清單股票代碼集合(命中標記用,W4條件)"""
    watchlist_codes = watchlist_codes or set()

    # 掃描範圍:固定觀察池(使用者10群組) 或 全市場廣掃
    if getattr(C, "USE_FIXED_UNIVERSE", False):
        codes = list(C.UNIVERSE)
    else:
        codes = broker.market_scan_codes()
    snaps = broker.batch_snapshots(codes)

    # 硬過濾:流動性(只對全市場廣掃生效;固定觀察池是使用者定案,不過濾)
    if not getattr(C, "USE_FIXED_UNIVERSE", False):
        snaps = [s for s in snaps
                 if (s["total_volume"] or 0) >= C.MIN_VOLUME_LOTS * 1000]

    # ① 資金流入板塊
    sectors = compute_sector_flow(snaps)
    locked = [s["name"] for s in sectors if s["locked"]]

    # 評分上下文:載入自學習權重、市場模式、族群中位、ABAB狀態
    import db as _db
    try:
        scoring.load_weights(_db.load_factor_weights())
    except Exception:
        pass
    idx = broker.index_snapshot()
    market_pct = idx.get("index_pct") or 0.0
    up_n_pre = len([s for s in sectors if s["pct"] > 0])
    score_pre = min(100, int(up_n_pre / max(1, len(sectors)) * 70 + len(locked) * 10))
    mode = "attack" if score_pre >= 60 else ("caution" if score_pre >= 40 else "risk")
    sec_median = {s["name"]: s["pct"] for s in sectors}
    abab_a = set()
    try:
        for s in sectors:
            h = _db.sector_history(s["name"], days=5)
            from after_hours import _is_abab
            if _is_abab(h[:-1] if h else [], (h[-1]["pct"] if h else 0)) \
                    and h and h[-1]["pct"] < 0:
                abab_a.add(s["name"])       # 昨為B日 → 今偏A日
    except Exception:
        pass

    # ②③④ 前三族群龍頭深度分析
    leaders = [analyze_leader(l) for l in pick_leaders(sectors)]

    # ⑤ 其餘候選(W 條件)→ 熱力表
    leader_codes = {l["code"] for l in leaders}
    table = []
    _EC = {"strong": "entry_high", "hot": "entry", "warm": "potential",
           "neutral": "monitor", "cold": "risk"}
    for s in snaps:
        if "sector" not in s:
            continue
        in_wl = s["code"] in watchlist_codes
        is_leader = s["code"] in leader_codes
        # 龍頭股先評估(法人買超/外資連買是「進場理由」,
        # 即使跌破均價也應是 watch 不是 sell)
        row = eval_stock(
            s, locked,
            sector_median=sec_median.get(s.get("sector"), 0.0),
            market_pct=market_pct,
            abab_a_day=s.get("sector") in abab_a,
            mode=mode,
            is_leader=is_leader)
        row["is_watchlist_hit"] = in_wl
        row["is_leader"] = is_leader
        row["event_class"] = _EC[row["heat_level"]]
        table.append(row)
        if is_leader:
            continue   # 龍頭不入下面迴圈(避免重複)
    table.sort(key=lambda x: x["ai_score"], reverse=True)

    # 現金閘門(持股標記 hold + 滿手時進場降級)
    import gatekeeper
    table, gated, gate_note = gatekeeper.apply_gate(table)

    up_n = len([s for s in sectors if s["pct"] > 0])
    score = min(100, int(up_n / max(1, len(sectors)) * 70 + len(locked) * 10))
    now = datetime.now(TW_TZ)

    return {
        "market": {**broker.index_snapshot(),
                   "score": score,
                   "mode": "attack" if score >= 60 else
                           ("caution" if score >= 40 else "risk"),
                   "time": now.strftime("%H:%M:%S")},
        "sectors": [{k: v for k, v in s.items() if k != "members"}
                    for s in sectors[:8]],
        "locked_sectors": locked,
        "leaders": leaders,              # ★ 前三族群龍頭深度分析
        "stocks": table[:60],
        "gate": {"active": gated, "note": gate_note},
        "updated_at": now.isoformat(),
        "source": "Shioaji realtime + FinMind chips(EOD)",
        "is_market_hours": (now.weekday() < 5
                            and "09:00" <= now.strftime("%H:%M") <= "13:35"),
        # 私有欄位(server 用於盤後複查,回應前端前剝除)
        "_snaps": snaps,
        "_sectors_full": [{k: v for k, v in s.items() if k != "members"}
                          for s in sectors],
    }
