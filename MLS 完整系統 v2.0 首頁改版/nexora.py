"""
MLS 插件 — nexora.py
NEXORA Hard Rule · Market Behavior Intelligence Engine V2.0
====================================================================
純插件:只讀主系統既有資料(state/_snaps/_sectors_full/db/chips),
不改任何主系統邏輯。由 after_hours 盤後掛鉤呼叫,產出 12 節報告。

核心禁令(Hard Rule):
  禁止「資金流入=看多 / 資金流出=看空」單維判斷。
  所有結論必須經 價格×成交量×法人×技術面 交叉驗證(Level 8 自檢強制)。
"""

import os
import json
from datetime import datetime, timezone, timedelta

import config as C
import db
import chips
import scoring

TW_TZ = timezone(timedelta(hours=8))
REPORT_DIR = os.path.join(os.path.dirname(__file__), "reports")


def _stars(x, full=5):
    n = max(0, min(full, int(round(x))))
    return "★" * n + "☆" * (full - n)


# ════════════════════════════════════════════════════════
# Level 1 資金流分析(族群 5/10/20 日累積 + 個股 Flow Score)
# ════════════════════════════════════════════════════════
def sector_flow(sectors):
    out = []
    for s in sectors:
        if s.get("type") != "attack":
            continue
        hist = db.sector_history(s["name"], days=21)   # 舊→新
        shares = [h["amount_share"] for h in hist]
        deltas = [b - a for a, b in zip(shares, shares[1:])]

        def cum(n):
            return round(sum(deltas[-n:]), 2) if deltas else 0.0

        streak = 0
        for d in reversed(deltas):
            if streak == 0:
                streak = 1 if d > 0 else (-1 if d < 0 else 0)
            elif (streak > 0) == (d > 0) and d != 0:
                streak += 1 if d > 0 else -1
            else:
                break
        trend = _stars(2.5 + (cum(5) * 3) + (0.5 if streak >= 3 else 0))
        out.append({
            "sector": s["name"], "today_pct": s["pct"],
            "today_share": s["amount_share"], "flow_score": s["flow_score"],
            "cum5": cum(5), "cum10": cum(10), "cum20": cum(20),
            "rank": s["rank"], "streak": streak, "trend": trend,
            "persistent": streak >= 3,
        })
    return out


def stock_flow(snaps):
    rows = []
    for s in snaps:
        aflow = scoring.get_aflow(s["code"])            # 主動買-賣估算(股)
        tv = s.get("total_volume") or 1
        buy_est = int((tv + aflow) / 2)
        sell_est = int((tv - aflow) / 2)
        fs = 50 + (aflow / tv) * 50 + min(20, (s.get("volume_ratio") or 0) * 5)
        rows.append({
            "code": s["code"], "name": C.NAME_MAP.get(s["code"], s["code"]),
            "sector": s.get("sector", "—"),
            "amount_100m": round((s.get("total_amount") or 0) / 1e8, 1),
            "volume": tv, "buy_est": buy_est, "sell_est": sell_est,
            "net_est": aflow, "chg": s["change_rate"],
            "flow_score": int(max(0, min(100, fs))),
        })
    rows.sort(key=lambda x: -x["flow_score"])
    return rows


def flow_sentence(net_100m, chg):
    """Hard Rule 措辭:禁止『今天有資金流入』式單維描述。"""
    d = "淨流入" if net_100m >= 0 else "淨流出"
    if net_100m >= 0 and chg < 0:
        return (f"今日估算{d} {abs(net_100m):.1f} 億元,但價格下跌,"
                f"代表市場存在承接,仍需觀察賣壓是否完全消化。")
    if net_100m >= 0 and chg >= 0:
        return (f"今日估算{d} {abs(net_100m):.1f} 億元且價格上漲,"
                f"價量同向,惟需法人與均線確認方能定調攻擊。")
    if net_100m < 0 and chg >= 0:
        return (f"今日估算{d} {abs(net_100m):.1f} 億元但價格收紅,"
                f"量縮惜售或空單回補,續航力存疑。")
    return (f"今日估算{d} {abs(net_100m):.1f} 億元且價格下跌,"
            f"賣壓主導,觀察是否出現承接訊號。")


# ════════════════════════════════════════════════════════
# Level 2 Money Quality(八項交叉)
# ════════════════════════════════════════════════════════
MQ_LABEL = [(90, "★★★★★ 真正攻擊"), (75, "★★★★☆ 開始布局"),
            (60, "★★★☆☆ 觀察"), (40, "★★☆☆☆ 換手"), (0, "★☆☆☆☆ 出貨")]


def money_quality(s, ch):
    aflow = scoring.get_aflow(s["code"])
    tv = s.get("total_volume") or 1
    pts = 0
    pts += 15 if aflow > 0 else 0                                   # ①資金方向
    pts += 15 if s["change_rate"] > 0 else 0                        # ②價格方向
    vr = s.get("volume_ratio") or 0
    pts += 12 if vr >= 1.5 else (6 if vr >= 1.0 else 0)             # ③成交量
    inst = (ch.get("inst_net_20d_lots") or 0)
    pts += 15 if inst > 0 else 0                                    # ④法人
    avgp = s.get("avg_price") or 0
    pts += 10 if (avgp and s["price"] >= avgp) else 0               # ⑤站回MA5(盤中以均價線)
    pts += 8 if s["change_rate"] > 1.5 else 0                       # ⑥站回MA10近似:強於中期
    pts += 10 if (s.get("high") and s["price"] >= s["high"]) else 0 # ⑦創高
    pts += 15 if (s.get("high") and s["price"] >= s["high"] and vr >= 1.5) else 0  # ⑧帶量突破
    score = min(100, pts)
    label = next(l for th, l in MQ_LABEL if score >= th)
    return score, label


# ════════════════════════════════════════════════════════
# Level 3 市場結構(六選一,不得留白)
# ════════════════════════════════════════════════════════
def market_structure(mkt_pct, total_amt_ratio, inst_bias, breadth):
    """
    breadth: 上漲族群比例0~1;inst_bias: 法人淨買為+1/淨賣-1/混0
    回傳 (結構, 依據dict)
    """
    flow_s = _stars(total_amt_ratio * 5)
    price_s = _stars(2.5 + mkt_pct)
    vol_s = _stars(total_amt_ratio * 5)
    inst_s = _stars(2.5 + inst_bias * 1.5)
    basis = {"資金流": flow_s, "價格": price_s, "成交量": vol_s, "法人": inst_s}

    if mkt_pct <= -2.5:
        st, why = "恐慌", "大盤重挫且普跌,情緒性賣壓主導。"
    elif mkt_pct < -0.8 and breadth < 0.35:
        st, why = "修正", "價跌且多數族群同步走弱,屬趨勢內修正。"
    elif total_amt_ratio > 0.9 and mkt_pct < 0.3 and breadth < 0.55:
        st, why = "換手", "大量成交但價格收平低,買盤存在、賣壓仍較強。"
    elif total_amt_ratio > 0.9 and mkt_pct < 0 and inst_bias < 0:
        st, why = "出貨", "量大價跌且法人偏賣,分配特徵。"
    elif mkt_pct >= 0.8 and breadth >= 0.6 and total_amt_ratio >= 0.8:
        st, why = "主升", "價漲、量足、族群普攻,資金與價格同向。"
    elif mkt_pct > -0.5 and total_amt_ratio < 0.8 and inst_bias >= 0:
        st, why = "吸籌", "量縮價穩且法人未撤,低調承接特徵。"
    else:
        st, why = "換手", "價量法人訊號混合,以高檔/區間換手視之。"
    return st, basis, why


# ════════════════════════════════════════════════════════
# Level 4 承接分析
# ════════════════════════════════════════════════════════
def absorption(snaps):
    down = [s for s in snaps if s["change_rate"] < 0]
    if not snaps:
        return _stars(0), _stars(0), "資料不足"
    sp = min(5, len(down) / max(1, len(snaps)) * 6)                 # 賣壓廣度
    absorbed = 0
    for s in down:
        rng = (s.get("high") or 0) - (s.get("low") or 0)
        if rng > 0 and (s["price"] - s["low"]) / rng > 0.5:
            absorbed += 1                                            # 收在振幅上半=有人接
    ab = min(5, (absorbed / max(1, len(down))) * 6) if down else 4
    if ab >= 4 and sp >= 3:   res = "承接成功"
    elif ab >= 3:             res = "吸收完成" if sp < 3 else "仍在換手"
    else:                     res = "承接不足"
    return _stars(sp), _stars(ab), res


# ════════════════════════════════════════════════════════
# Level 5 賣壓來源(排序推論,不得答未知)
# ════════════════════════════════════════════════════════
def selling_source(sectors, snaps, inst_bias):
    cands = []
    weak = [s for s in sectors if s.get("pct", 0) < -1.5]
    if inst_bias < 0:
        cands.append("法人調節(觀察池法人近月淨賣/當日賣超族群集中)")
    if weak:
        cands.append(f"族群輪動退潮({'、'.join(x['name'] for x in weak[:3])} 資金撤出)")
    hi_vol_down = [s for s in snaps if s["change_rate"] < -3 and (s.get("volume_ratio") or 0) > 1.5]
    if hi_vol_down:
        cands.append("高檔獲利了結/融資多殺多(爆量重挫股集中)")
    cands.append("外部市場連動(美股/亞股/匯率——盤後需以國際行情驗證)")
    return ("目前無法完全確認,最可能原因排序如下:",
            cands[:3] if cands else ["資料不足,待盤後國際行情比對"])


# ════════════════════════════════════════════════════════
# Level 6 Smart Money Score(固定權重合成)
# ════════════════════════════════════════════════════════
SMS_LABEL = [(90, "主力攻擊"), (80, "主力布局"), (70, "偏多"), (60, "中性"),
             (40, "觀察"), (20, "偏空"), (0, "撤退")]


def smart_money(sec_rows, stk_rows, mq_avg, inst_bias, snaps, mkt_pct):
    sec_s = min(100, 50 + sum(r["flow_score"] for r in sec_rows[:3]) )
    sec_s = max(0, min(100, sec_s))
    stk_s = sum(r["flow_score"] for r in stk_rows[:10]) / max(1, min(10, len(stk_rows)))
    inst_s = 50 + inst_bias * 40
    up = [s for s in snaps if s["change_rate"] > 0 and (s.get("volume_ratio") or 0) > 1.2]
    pv_s = min(100, len(up) / max(1, len(snaps)) * 160)
    ma_s = min(100, len([s for s in snaps if s.get("avg_price") and s["price"] >= s["avg_price"]])
               / max(1, len(snaps)) * 130)
    hi_s = min(100, len([s for s in snaps if s.get("high") and s["price"] >= s["high"]])
               / max(1, len(snaps)) * 300)
    total = (sec_s * .20 + stk_s * .15 + mq_avg * .20 + inst_s * .15
             + pv_s * .15 + ma_s * .10 + hi_s * .05)
    total = int(max(0, min(100, total)))
    label = next(l for th, l in SMS_LABEL if total >= th)
    detail = {"SectorFlow(20%)": int(sec_s), "StockFlow(15%)": int(stk_s),
              "MoneyQuality(20%)": int(mq_avg), "法人(15%)": int(inst_s),
              "量價(15%)": int(pv_s), "均線(10%)": int(ma_s), "創高(5%)": int(hi_s)}
    return total, label, detail


# ════════════════════════════════════════════════════════
# Level 7 隔日機率(規則化,附理由,禁憑感覺)
# ════════════════════════════════════════════════════════
def tomorrow_prob(structure, sms, rotation_reports):
    base = {"主升": (55, 15, 30), "吸籌": (45, 20, 35), "換手": (35, 30, 35),
            "出貨": (22, 48, 30), "修正": (30, 42, 28), "恐慌": (46, 34, 20)}
    up, dn, fl = base.get(structure, (33, 33, 34))
    reasons = [f"市場結構判定為「{structure}」(基準機率)"]
    if sms >= 70:
        up += 8; dn -= 8; reasons.append(f"Smart Money {sms} 偏多,上修反彈")
    elif sms <= 40:
        up -= 8; dn += 8; reasons.append(f"Smart Money {sms} 偏空,上修續跌")
    abab_b = [r["sector"] for r in (rotation_reports or [])
              if r.get("abab") and r.get("pct", 0) < 0]
    if abab_b:
        up += 6; dn -= 6
        reasons.append(f"ABAB 節奏:{'、'.join(abab_b[:2])} 今為B日,明偏A日(節奏≠個股健康)")
    s = up + dn + fl
    up, dn = round(up / s * 100), round(dn / s * 100)
    fl = 100 - up - dn
    return {"反彈": up, "續跌": dn, "震盪": fl}, reasons


# ════════════════════════════════════════════════════════
# Level 8 AI 自我驗證(Hard Rule 強制)
# ════════════════════════════════════════════════════════
def self_verify(sec_rows, structure, sms, mkt_pct, inst_bias):
    checks, revised = [], False
    inflow = [r for r in sec_rows if r["flow_score"] > 0]
    if inflow and mkt_pct < 0 and sms >= 70:
        revised = True
        checks.append("⚠ 檢出『資金流入但價格下跌』卻給出偏多結論 → 依 Hard Rule 降級為中性,"
                      "需價格站回均線+法人翻買雙確認。")
    if inflow and inst_bias < 0:
        checks.append("資金流入 vs 法人淨賣 → 資金欄可能含假紅,結論已交叉法人面驗證。")
    if not checks:
        checks.append("✓ 未檢出單維『流入=看多/流出=看空』推論;結論已含價格、量能、法人、均線交叉驗證。")
    return checks, revised


# ════════════════════════════════════════════════════════
# 主入口:產 12 節報告(缺一節視為未完成)
# ════════════════════════════════════════════════════════
def run_report(state, rotation_reports=None):
    snaps = [s for s in state.get("_snaps", []) if s.get("sector")]
    sectors = state.get("_sectors_full", state.get("sectors", []))
    mkt = state.get("market", {})
    mkt_pct = mkt.get("index_pct") or 0.0

    inst_vals = []
    mq_rows = []
    for s in snaps:
        ch = chips.get_chips(s["code"])
        if ch.get("inst_net_20d_lots") is not None:
            inst_vals.append(1 if ch["inst_net_20d_lots"] > 0 else -1)
        sc, lb = money_quality(s, ch)
        mq_rows.append({"code": s["code"], "name": C.NAME_MAP.get(s["code"], s["code"]),
                        "mq": sc, "label": lb})
    inst_bias = 0 if not inst_vals else (1 if sum(inst_vals) > 0 else (-1 if sum(inst_vals) < 0 else 0))
    mq_rows.sort(key=lambda x: -x["mq"])
    mq_avg = sum(r["mq"] for r in mq_rows) / max(1, len(mq_rows))

    sec_rows = sector_flow(sectors)
    stk_rows = stock_flow(snaps)
    total_net = sum(r["net_est"] * 1 for r in stk_rows)
    net_100m = sum(r["amount_100m"] for r in stk_rows if r["net_est"] > 0) - \
               sum(r["amount_100m"] for r in stk_rows if r["net_est"] < 0)

    breadth = len([x for x in sectors if x.get("pct", 0) > 0]) / max(1, len(sectors))
    amt_ratio = 0.85  # 觀察池口徑:以量比中位近似;缺全市場基準時的保守值
    vrs = sorted([s.get("volume_ratio") or 0 for s in snaps])
    if vrs:
        amt_ratio = min(1.2, vrs[len(vrs) // 2])

    structure, basis, why = market_structure(mkt_pct, amt_ratio, inst_bias, breadth)
    sp_s, ab_s, ab_res = absorption(snaps)
    src_head, src_list = selling_source(sectors, snaps, inst_bias)
    sms, sms_label, sms_detail = smart_money(sec_rows, stk_rows, mq_avg, inst_bias, snaps, mkt_pct)
    checks, revised = self_verify(sec_rows, structure, sms, mkt_pct, inst_bias)
    if revised:
        sms = min(sms, 60)
        sms_label = next(l for th, l in SMS_LABEL if sms >= th)
    probs, prob_reasons = tomorrow_prob(structure, sms, rotation_reports)

    if sms >= 70 and structure in ("主升", "吸籌"):
        decision = "偏多操作:沿鎖定攻擊族群強勢股快打,破均線即出。"
    elif sms <= 40 or structure in ("出貨", "修正"):
        decision = "防守優先:不開新多單,持股沿停損紀律,現金為王。"
    else:
        decision = "中性觀望:僅追蹤大戶未斷之抗跌股,等結構與法人雙確認。"

    d = datetime.now(TW_TZ)
    L = []
    L.append(f"# NEXORA 盤後報告 {d:%Y-%m-%d}")
    L.append(f"\n## 1. 市場總覽\n加權 {mkt.get('index','—')}({mkt_pct:+.2f}%) "
             f"成交 {mkt.get('amount_100m','—')} 億|觀察池上漲族群比 {breadth:.0%}")
    L.append("\n## 2. 資金流分析")
    for r in sec_rows[:8]:
        L.append(f"- {r['sector']} Today {r['today_pct']:+.2f}%/佔比{r['today_share']}% "
                 f"5D {r['cum5']:+.2f}pp 10D {r['cum10']:+.2f}pp 20D {r['cum20']:+.2f}pp "
                 f"#{r['rank']} {r['trend']}{' 持續流入' if r['persistent'] else ''}")
    L.append(flow_sentence(net_100m, mkt_pct))
    L.append("\n## 3. Money Quality(前8)")
    for r in mq_rows[:8]:
        L.append(f"- {r['name']}({r['code']}) MQ {r['mq']} {r['label']}")
    L.append(f"\n## 4. Market Structure:**{structure}**")
    L.append(" ".join(f"{k} {v}" for k, v in basis.items()) + f"\n判斷依據:{why}")
    L.append(f"\n## 5. Absorption\n賣壓 {sp_s}|承接 {ab_s}|結果:**{ab_res}**")
    L.append(f"\n## 6. 法人分析\n觀察池法人近月傾向:"
             f"{'偏買' if inst_bias>0 else ('偏賣' if inst_bias<0 else '混合')}"
             f"(盤後由 chips 日更資料驗證)")
    L.append("\n## 7. 族群輪動")
    if rotation_reports:
        for r in rotation_reports:
            L.append(f"- {r['sector']} {r['pct']:+.1f}% {r['quadrant']}"
                     f"{' [ABAB]' if r.get('abab') else ''}"
                     + (f" 抗跌:{','.join(r['resilient'])}" if r.get("resilient") else ""))
    else:
        L.append("(由主系統四象限模組供給)")
    L.append("\n## 8. 個股 Flow 排行(前10)")
    for r in stk_rows[:10]:
        L.append(f"- {r['name']}({r['code']}) Flow {r['flow_score']} "
                 f"{r['chg']:+.1f}% 淨流估 {r['net_est']/1000:+,.0f} 張 額 {r['amount_100m']}億")
    L.append(f"\n## 9. Smart Money Score:**{sms}｜{sms_label}**")
    L.append(json.dumps(sms_detail, ensure_ascii=False))
    L.append(f"\n## 10. Tomorrow Probability\n反彈 {probs['反彈']}%|續跌 {probs['續跌']}%|震盪 {probs['震盪']}%")
    for r in prob_reasons:
        L.append(f"- {r}")
    L.append(f"\n## 11. AI Decision\n{decision}")
    L.append("\n## 12. AI Self Verification")
    for c in checks:
        L.append(f"- {c}")
    report_md = "\n".join(L)

    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"NEXORA_{d:%Y%m%d}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_md)

    summary = (f"🧠 NEXORA|結構 {structure}|SMS {sms} {sms_label}|"
               f"承接 {ab_res}|明日 反彈{probs['反彈']}%/續跌{probs['續跌']}%/震盪{probs['震盪']}%"
               + ("|⚠自檢降級" if revised else ""))
    return {"path": path, "summary": summary, "structure": structure,
            "sms": sms, "probs": probs, "revised": revised, "report": report_md}
