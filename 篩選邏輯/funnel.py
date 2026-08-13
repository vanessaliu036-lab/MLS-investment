"""
funnel.py — 逐層淘汰漏斗(取代 screen_post + screen_intraday 的排序取前 N)

===== 為什麼要重寫 =====

舊版用「評分後排序取前 20」,上限寫死在 screen_post。
所以名單永遠是 20 檔 —— 那不是篩選,是湊數。
你截圖那份 20 檔全「觀察」軌,正是這個機制的產物。

漏斗是「符合條件的剩下這些」,排序取前 N 是「不管怎樣都給你 20 檔」。
兩者完全不同。

===== 硬性規則 =====

規則 1:每一層都是通過/淘汰的判斷,不是評分排序。
        分數只用來排序「留下來的那些」,不決定誰留下。

規則 2:逐層數字必須嚴格遞減。任兩層相同 = 那層沒作用,系統自己標出來。

規則 3:留下幾檔就是幾檔。可能 3 檔、可能 0 檔。
        空清單是合法輸出,顯示「今日無符合標的」。
        禁止補位、禁止降門檻湊數。

規則 4:每層記錄淘汰理由分布。兩週後靠這個決定放鬆哪一條。

規則 5:NO_DATA 不等於淘汰。缺資料的項目不計入淘汰票數。

===== 流程 =====

    09:00–13:20  持續評估,不定案。畫面標「評估中」,不給進場依據。
    13:20        定案快照,執行 L1 → L2 → L2.5
    盤後籌碼到位  執行 L3,產出明日清單(0～N 檔)

L1  減量層:砍掉今天沒戲的(中一項即淘汰)
L1.5 背離否決:漲但資金流出 → 直接淘汰,不論後面分數多高
     (必須排在核心層之前,否則會被四判準先砍掉,
      你就看不到「有幾檔是因為邊拉邊出被踢」這個統計)
L2  核心層:四判準中兩項才留(這層決定命中率)
L3  籌碼層:三大法人拆開看,連續天數權重高於單日金額
"""

from __future__ import annotations

import datetime as _dt
import json

import store
import layered_score
import recovery_scan
from envelope import run_all, persist_status, missing_labels
from phase import Phase, prev_trading_day, today_tw

PLUGIN = "funnel"
TABLE = "funnel_result"
LOG_TABLE = "funnel_log"

DECIDE_HOUR, DECIDE_MIN = 13, 20     # 盤中定案時刻
BLIND_MIN = 15                        # 鐵律1:開盤 15 分鐘不納入判斷

# ---- L1 門檻
L1_DROP_VOTES = 1   # 三項任一成立即淘汰。設 2 會導致這層永不作用(實測 51→51)
L1_VOL_WEAK = 0.6          # 量能低於昨同時段此倍數
L1_REL_WEAK = -2.0         # 弱於族群此百分比

# ---- L2 門檻
L2_KEEP_VOTES = 2          # 四判準中幾項成立才留
L2_PERSIST = 0.70          # aflow 正值區間占比
L2_SINGLE_CAP = 0.40       # 單一區間佔全天量上限
L2_DIP_MIN = 1.5           # 回落幅度 %
L2_DIP_SHRINK = 0.60       # 回落期間量能萎縮比
L2_DIP_RECOVER = 0.50      # 收復比例
L2_REL_STRONG = 1.5        # 強於族群 %
L2_VOL_SURGE = 1.8         # 量增倍數
L2_STABLE_LO, L2_STABLE_HI = 1.0, 5.0

# ---- L2.5 背離否決
DIVERGE_UP_PCT = 1.0       # 漲幅超過此值
DIVERGE_OUTFLOW = 0        # 且 aflow 低於此值 → 邊拉邊出

# ---- L3 籌碼(連續天數權重 > 單日金額,依你的實際標準)
L3_W = {
    "foreign_streak": 30,   # 外資連續天數
    "trust_streak": 25,     # 投信連續天數(投信連買是最強訊號之一)
    "margin": 20,           # 融資減
    "foreign_net": 15,      # 外資單日金額(權重刻意低於連續天數)
    "dealer": 10,
}


def _usable(series: list[dict]) -> list[dict]:
    """濾掉開盤 15 分鐘(鐵律1:開盤壓低不算數)。"""
    return [s for s in series
            if int(s["slot"][:2]) * 60 + int(s["slot"][2:]) >= 9 * 60 + BLIND_MIN]


def central_keep(classified: dict) -> bool:
    """兩條管線唯一去留邊界：只有中央分類器的四重失效可以淘汰。"""
    return classified.get("classification") != layered_score.TIER_REJECTED


def _latest_flow(series: list[dict]):
    vals = [row.get("net_active") for row in _usable(series)
            if row.get("net_active") is not None]
    return vals[-1] if vals else None


# ================================================================ L1 減量層

def layer1(code: str, series: list[dict], bar_y: dict | None,
           group_avg: dict[str, float], group: str | None) -> tuple[bool, list[str]]:
    """
    砍掉今天沒戲的。三項任一成立即淘汰。
    NO_DATA 不計入票數 —— 資料沒接入不該被誤殺。
    """
    votes: list[str] = []
    s = _usable(series)
    if not s:
        return True, []          # 沒資料 → 不淘汰,留給下一層

    last = s[-1]
    mins = int(last["slot"][:2]) * 60 + int(last["slot"][2:]) - 9 * 60

    # 量能不足
    # 單位對齊：b_snapshot.volume 來自 Shioaji＝「張」,daily_bar.volume(昨量 yv)
    # collect 寫的是 FinMind Trading_Volume＝「股」,差 1000 倍。不換算會讓 pace
    # 永遠 ≈0 → 全池誤殺「量能不足」(2026-08-04 驗證發現)。故盤中張×1000 對齊股。
    yv = (bar_y or {}).get("volume")
    if last.get("volume") is not None and yv and mins > 0:
        frac = min(1.0, max(0.05, mins / 270))
        pace = (last["volume"] * 1000) / max(1.0, yv * frac)
        if pace < L1_VOL_WEAK:
            votes.append("量能不足")

    # 跌破月線未收復
    ma20 = (bar_y or {}).get("ma20")
    if last.get("price") is not None and ma20:
        if last["price"] < ma20:
            votes.append("跌破月線")

    # 弱於族群
    if group and group in group_avg and last.get("change_rate") is not None:
        if last["change_rate"] - group_avg[group] <= L1_REL_WEAK:
            votes.append("弱於族群")

    # V3:減量條件只記特徵。最終去留統一交給 layered_score 四重閘門。
    return True, votes


# ================================================================ L2 核心層

def _c_persist(series) -> tuple[bool | None, str]:
    s = _usable(series)
    vals = [x["net_active"] for x in s if x.get("net_active") is not None]
    if len(vals) < 6:
        return None, "時序不足"
    ratio = sum(1 for v in vals if v > 0) / len(vals)
    if ratio < L2_PERSIST:
        return False, f"aflow 正值占比 {ratio:.0%}"
    vols = [x["volume"] for x in s if x.get("volume") is not None]
    if len(vols) >= 2:
        d = [max(0, vols[i] - vols[i - 1]) for i in range(1, len(vols))]
        if sum(d) > 0 and max(d) / sum(d) > L2_SINGLE_CAP:
            return False, f"單一區間佔量 {max(d)/sum(d):.0%},疑單筆大單"
    return True, f"aflow 持續為正 {ratio:.0%}"


def _c_dip(series) -> tuple[bool | None, str]:
    s = _usable(series)
    pts = [(x.get("price"), x.get("volume")) for x in s if x.get("price") is not None]
    if len(pts) < 8:
        return None, "時序不足"
    pr = [p for p, _ in pts]
    pi = max(range(len(pr)), key=lambda i: pr[i])
    if pi >= len(pr) - 3:
        return False, "高點在尾段,無回落收復"
    peak = pr[pi]
    after = pr[pi:]
    ti = pi + min(range(len(after)), key=lambda i: after[i])
    trough = pr[ti]
    dip = (peak - trough) / peak * 100
    if dip < L2_DIP_MIN:
        return False, f"回落僅 {dip:.1f}%"
    if trough <= min(pr) * 1.0005:
        return False, "回落破當日低"
    vo = [v for _, v in pts if v is not None]
    if len(vo) == len(pts) and ti > pi:
        rv = [max(0, vo[i] - vo[i-1]) for i in range(1, pi + 1)]
        dv = [max(0, vo[i] - vo[i-1]) for i in range(pi + 1, ti + 1)]
        if rv and dv:
            ra, da = sum(rv)/len(rv), sum(dv)/len(dv)
            if ra > 0 and da / ra > L2_DIP_SHRINK:
                return False, f"回落量未縮({da/ra:.0%}),像真賣壓"
    rec = (pr[-1] - trough) / (peak - trough) if peak > trough else 0
    if rec < L2_DIP_RECOVER:
        return False, f"僅收復 {rec:.0%}"
    return True, f"回落 {dip:.1f}% 量縮不破低,收復 {rec:.0%}"


def _c_rel(series, group_avg, group) -> tuple[bool | None, str]:
    s = _usable(series)
    if not s or s[-1].get("change_rate") is None:
        return None, "無漲跌幅"
    if not group or group not in group_avg:
        return None, "無族群資料"
    diff = s[-1]["change_rate"] - group_avg[group]
    if diff < L2_REL_STRONG:
        return False, f"僅強於族群 {diff:+.1f}%"
    return True, f"強於族群 {diff:+.1f}%"


def _c_volstable(series, bar_y) -> tuple[bool | None, str]:
    s = _usable(series)
    yv = (bar_y or {}).get("volume")
    if not s or s[-1].get("volume") is None or not yv:
        return None, "無量能基準"
    last = s[-1]
    mins = int(last["slot"][:2]) * 60 + int(last["slot"][2:]) - 9 * 60
    frac = min(1.0, max(0.05, mins / 270))
    # 同 layer1:b_snapshot(張) 對齊 daily_bar(股),張×1000,否則 pace 恆≈0 → 量增判準永不成立。
    pace = (last["volume"] * 1000) / max(1.0, yv * frac)
    if pace < L2_VOL_SURGE:
        return False, f"量能 {pace:.1f} 倍"
    cr = last.get("change_rate")
    if cr is None:
        return None, "無漲跌幅"
    if not (L2_STABLE_LO <= cr <= L2_STABLE_HI):
        return False, f"量增但漲幅 {cr:+.1f}% 不在價穩區間"
    return True, f"量 {pace:.1f} 倍且價穩 {cr:+.1f}%"


def layer2(code, series, bar_y, group_avg, group) -> tuple[bool, dict, int]:
    checks = {
        "持續性": _c_persist(series),
        "下殺承接": _c_dip(series),
        "相對強度": _c_rel(series, group_avg, group),
        "量增價穩": _c_volstable(series, bar_y),
    }
    hits = sum(1 for ok, _ in checks.values() if ok is True)
    detail = {k: {"pass": ok, "why": why} for k, (ok, why) in checks.items()}
    return hits >= L2_KEEP_VOTES, detail, hits


# ================================================================ L2.5 背離否決

def layer25(series) -> tuple[bool, str]:
    """
    漲但資金流出 = 邊拉邊出。直接淘汰,不論前面分數多高。

    盤後只看得到「漲了 10%」,看不到資金在流出。
    籌碼面淨額也會把「誰在賣」這個資訊消掉。
    這一層就是補這個盲點。
    """
    s = _usable(series)
    if not s:
        return True, ""
    last = s[-1]
    cr, na = last.get("change_rate"), last.get("net_active")
    if cr is None or na is None:
        return True, ""      # NO_DATA 不淘汰
    if cr >= DIVERGE_UP_PCT and na < DIVERGE_OUTFLOW:
        return True, f"邊拉邊出:漲 {cr:+.1f}% 但資金流出 {na/1e4:,.0f} 萬"
    return True, ""


# ================================================================ L3 籌碼層

def layer3(code, inst: dict | None, margin: dict | None) -> tuple[bool, list[str], float]:
    """
    三大法人拆開看,不用加總。淨額會把「誰在賣」消掉。
    連續天數權重高於單日金額 —— 依你的實際觀察標準。
    """
    if inst is None:
        return True, ["籌碼未到位"], 0.0

    reasons: list[str] = []
    score = 0.0

    f_net = inst.get("foreign_net")
    t_net = inst.get("trust_net")
    d_net = inst.get("dealer_net")
    f_days = inst.get("foreign_days")
    t_days = inst.get("trust_days")

    if f_net is not None and t_net is not None and f_net < 0 and t_net < 0:
        reasons.append(f"外資賣 {abs(f_net)}、投信賣 {abs(t_net)},同步賣超")

    if f_days is not None:
        if f_days > 0:
            score += L3_W["foreign_streak"] * min(1.0, f_days / 5)
            reasons.append(f"外資連買{f_days}日")
        elif f_days < 0:
            score -= L3_W["foreign_streak"] * min(1.0, -f_days / 5)
            reasons.append(f"外資連賣{-f_days}日")

    if t_days is not None:
        if t_days > 0:
            score += L3_W["trust_streak"] * min(1.0, t_days / 5)
            reasons.append(f"投信連買{t_days}日")
        elif t_days < 0:
            score -= L3_W["trust_streak"] * min(1.0, -t_days / 5)
            reasons.append(f"投信連賣{-t_days}日")

    if f_net is not None:
        if f_net > 0:
            score += L3_W["foreign_net"] * min(1.0, f_net / 5000)
            reasons.append(f"外資買超{f_net}張")
        else:
            reasons.append(f"外資賣超{abs(f_net)}張")

    if d_net is not None and d_net > 0:
        score += L3_W["dealer"] * min(1.0, d_net / 2000)

    if margin and margin.get("margin_change") is not None:
        ch = margin["margin_change"]
        if ch < 0:
            score += L3_W["margin"]
            reasons.append("融資減(散戶洗出)")
        else:
            score -= L3_W["margin"] * 0.5
            reasons.append("融資增")

    if not any("買" in r for r in reasons):
        reasons.append("無法人買進證據")

    return True, reasons, round(score, 1)


# ================================================================ 主流程

def _log_layer(d, layer, entered, survivors, reason_counts, when, db_path):
    store.upsert_intraday(LOG_TABLE, PLUGIN, [{
        "data_date": d.isoformat(), "layer": layer,
        "entered": entered, "survived": survivors,
        "dropped": entered - survivors,
        "reason_breakdown": json.dumps(reason_counts, ensure_ascii=False),
        "decided_at": when,
    }], db_path)


def run(universe: list[str], code_group: dict[str, str],
        db_path: str = "mls.db", data_date: _dt.date | None = None,
        with_chips: bool = True) -> dict:
    """
    執行完整漏斗。

    with_chips=False → 只跑到 L2.5(盤中定案,籌碼還沒到)
    with_chips=True  → 跑完 L3(盤後,產出明日清單)
    """
    d = data_date or today_tw()
    y = prev_trading_day(d)
    now = _dt.datetime.now().isoformat(timespec="seconds")

    import b_snapshot
    envs = run_all({
        "snapshots": lambda: b_snapshot.series_all(d, db_path),
        "bar_y": lambda: store.read_date("daily_bar", y, db_path),
        "bar_today": lambda: store.read_date("daily_bar", d, db_path),
        "aflow_y": lambda: store.read_date("aflow", y, db_path),
        "inst": lambda: store.read_date("inst_flow", d, db_path),
        "margin": lambda: store.read_date("margin", d, db_path),
    }, phase=Phase.POST if with_chips else Phase.INTRADAY)
    persist_status(envs, db_path)

    snaps = envs["snapshots"].get({}) or {}
    bar_y = envs["bar_y"].get({}) or {}
    bar_today = envs["bar_today"].get({}) or {}
    aflow_y = envs["aflow_y"].get({}) or {}
    inst = envs["inst"].get({}) or {}
    margin = envs["margin"].get({}) or {}

    # ---- 籌碼到位狀態(讓你分辨「還沒到」和「假裝有」)
    chips_status = _chips_status(inst, universe, d)

    if not snaps:
        return {
            "data_date": d.isoformat(),
            "purpose": "漏斗未執行 — 無盤中時序快照,請確認 b_snapshot 有在跑",
            "layers": [], "items": [], "chips": chips_status,
            "degraded": ["時序快照"],
        }

    # ---- 族群平均
    gsum: dict[str, list[float]] = {}
    for c, ser in snaps.items():
        u = _usable(ser)
        if u and u[-1].get("change_rate") is not None:
            gsum.setdefault(code_group.get(c, "?"), []).append(u[-1]["change_rate"])
    group_avg = {g: sum(v) / len(v) for g, v in gsum.items() if v}

    layers = []
    rows = []

    # ---------- L0 全集
    pool = [c for c in universe if c in snaps]
    layers.append({"layer": "L0 全集", "entered": len(universe),
                   "survived": len(pool), "reasons": {}})

    # ---------- 舊 L1/L1.5/L2/L3 僅保留診斷；不再逐層移除
    l1_keep, l1_reasons = [], {}
    for c in pool:
        ok, votes = layer1(c, snaps[c], bar_y.get(c), group_avg, code_group.get(c))
        if ok:
            l1_keep.append(c)
        else:
            for v in votes:
                l1_reasons[v] = l1_reasons.get(v, 0) + 1
        rows.append({"data_date": d.isoformat(), "layer": "L1", "code": c,
                     "survived": int(ok),
                     "reasons": json.dumps(votes, ensure_ascii=False),
                     "detail": "", "decided_at": now})
    _log_layer(d, "L1", len(pool), len(l1_keep), l1_reasons, now, db_path)
    layers.append({"layer": "L1 減量", "entered": len(pool),
                   "survived": len(l1_keep), "reasons": l1_reasons})

    # ---------- L1.5 背離否決(必須在核心層之前)
    l15_keep, l15_reasons = [], {}
    for c in l1_keep:
        ok, why = layer25(snaps[c])
        if ok:
            l15_keep.append(c)
        else:
            l15_reasons["邊拉邊出"] = l15_reasons.get("邊拉邊出", 0) + 1
        rows.append({"data_date": d.isoformat(), "layer": "L1.5", "code": c,
                     "survived": int(ok), "reasons": why,
                     "detail": "", "decided_at": now})
    _log_layer(d, "L1.5", len(l1_keep), len(l15_keep), l15_reasons, now, db_path)
    layers.append({"layer": "L1.5 背離否決", "entered": len(l1_keep),
                   "survived": len(l15_keep), "reasons": l15_reasons})

    # ---------- L2 核心（特徵診斷）
    l2_keep, l2_reasons, l2_detail = [], {}, {}
    for c in l15_keep:
        ok, detail, hits = layer2(c, snaps[c], bar_y.get(c),
                                  group_avg, code_group.get(c))
        l2_detail[c] = detail
        l2_keep.append(c)
        if not ok:
            k = f"僅{hits}項通過"
            l2_reasons[k] = l2_reasons.get(k, 0) + 1
        rows.append({"data_date": d.isoformat(), "layer": "L2", "code": c,
                     "survived": 1, "reasons": f"{hits}/4（特徵，不淘汰）",
                     "detail": json.dumps(detail, ensure_ascii=False),
                     "decided_at": now})
    _log_layer(d, "L2", len(l15_keep), len(l2_keep), l2_reasons, now, db_path)
    layers.append({"layer": "L2 核心", "entered": len(l15_keep),
                   "survived": len(l2_keep), "reasons": l2_reasons})

    final = l2_keep
    stage = "盤中定案"

    # ---------- L3 籌碼
    if with_chips:
        l3_keep, l3_reasons, l3_score = [], {}, {}
        for c in l2_keep:
            ok, why, sc = layer3(c, inst.get(c), margin.get(c))
            l3_keep.append(c)
            l3_score[c] = sc
            if not ok:
                for w in why:
                    key = w.split(":")[0][:12]
                    l3_reasons[key] = l3_reasons.get(key, 0) + 1
            rows.append({"data_date": d.isoformat(), "layer": "L3", "code": c,
                         "survived": 1,
                         "reasons": json.dumps(why, ensure_ascii=False),
                         "detail": "", "decided_at": now})
        _log_layer(d, "L3", len(l2_keep), len(l3_keep), l3_reasons, now, db_path)
        layers.append({"layer": "L3 籌碼", "entered": len(l2_keep),
                       "survived": len(l3_keep), "reasons": l3_reasons})
        l3_keep.sort(key=lambda c: -l3_score.get(c, 0))
        final = l3_keep
        stage = "盤後定案"

    # ---------- 中央分類器唯一去留門
    classified = {}
    central_final = []
    recovery_pool = []
    flow_now = {c: _latest_flow(snaps.get(c, [])) for c in final}
    flow_prev = {c: (aflow_y.get(c) or {}).get("net_active") for c in final}
    sector_turn = recovery_scan.sector_flow_turns(
        final, code_group, flow_now, flow_prev)
    for c in final:
        u = _usable(snaps[c])
        last = u[-1] if u else {}
        current_bar = dict(bar_today.get(c) or {})
        if not current_bar:
            current_bar = {
                "open": (u[0].get("price") if u else last.get("price")),
                "high": max((x.get("price") for x in u if x.get("price") is not None), default=last.get("price")),
                "low": min((x.get("price") for x in u if x.get("price") is not None), default=last.get("price")),
                "close": last.get("price"),
                "volume": (last.get("volume") or 0) * 1000,
                "ma5": (bar_y.get(c) or {}).get("ma5"),
                "ma20": (bar_y.get(c) or {}).get("ma20"),
                "ma60": (bar_y.get(c) or {}).get("ma60"),
                "vol_ma20": (bar_y.get(c) or {}).get("vol_ma20"),
            }
        lay = layered_score.score_layered(layered_score.build_input(
            c, current_bar, inst.get(c), change_rate=last.get("change_rate"),
            previous_bar=bar_y.get(c), aflow_today=_latest_flow(snaps[c]),
            aflow_previous=(aflow_y.get(c) or {}).get("net_active")))
        classified[c] = lay
        if central_keep(lay):
            central_final.append(c)
        else:
            prev_bar = bar_y.get(c) or {}
            rec = recovery_scan.scan(lay, {
                **current_bar,
                "change_rate": last.get("change_rate"),
                "total_net": (inst.get(c) or {}).get("total_net"),
                "aflow_today": flow_now.get(c),
                "aflow_previous": flow_prev.get(c),
                "previous_low": prev_bar.get("low"),
                "sector_flow_turn": sector_turn.get(c),
            })
            recovery_row = {
                "code": c, "group": code_group.get(c),
                "classification": lay["classification"],
                "failure_gates": lay["failure_gates"],
                "failure_gate_count": lay["failure_gate_count"],
                "recovery_status": rec["status"],
                "recovery_score": rec["score"],
                "recovery_signals": rec["signals"],
                "recovery_pending": rec["pending"],
                "recovery_trigger": rec["t1_trigger"],
                "recovery_pool": rec["in_recovery_pool"],
            }
            if rec["in_recovery_pool"]:
                recovery_pool.append(recovery_row)
    final = central_final

    store.upsert_intraday(TABLE, PLUGIN, rows, db_path)

    # ---- 規則 2:逐層必須嚴格遞減
    warnings = []
    for i in range(1, len(layers)):
        if layers[i]["survived"] == layers[i - 1]["survived"] and \
           layers[i]["entered"] > 0:
            warnings.append(f"{layers[i]['layer']} 未淘汰任何標的,該層可能沒作用")

    items = []
    for n, c in enumerate(final, 1):
        u = _usable(snaps[c])
        last = u[-1] if u else {}
        it = {
            "rank": n, "code": c, "group": code_group.get(c),
            "price": last.get("price"), "change_rate": last.get("change_rate"),
            "net_active": last.get("net_active"),
            "l2": l2_detail.get(c, {}),
            **{k: classified[c].get(k) for k in (
                "classification", "potential_grade", "trend_stage", "entry_status",
                "failure_gates", "failure_gate_count", "turn_signals")},
        }
        if with_chips:
            ok, why, sc = layer3(c, inst.get(c), margin.get(c))
            it["chip_score"] = sc
            it["chip_reasons"] = why
        items.append(it)

    return {
        "data_date": d.isoformat(),
        "stage": stage,
        "decided_at": now,
        "purpose": _purpose(stage, len(items), d, chips_status, with_chips),
        "layers": layers,
        "warnings": warnings,
        "chips": chips_status,
        "degraded": missing_labels(envs),
        "count": len(items),
        "items": items,
        "recovery_pool": sorted(recovery_pool,
                                key=lambda row: (-row["recovery_score"], row["code"])),
        "empty_is_valid": True,
    }


def _chips_status(inst: dict, universe: list[str], d: _dt.date) -> dict:
    """
    籌碼到位狀態。讓你分辨「還沒到」和「系統假裝有資料」。
    沒抓到就顯示沒抓到,禁止用昨日資料頂替。
    """
    if not inst:
        return {"ready": False, "fetched_at": None, "source": None,
                "coverage": f"0/{len(universe)}",
                "text": f"籌碼面:尚未取得(官方通常 15:30 後更新)· 0/{len(universe)} 檔"}
    fetched = [v.get("fetched_at") for v in inst.values() if v.get("fetched_at")]
    src = {v.get("source") for v in inst.values() if v.get("source")}
    first = min(fetched) if fetched else None
    return {
        "ready": True, "fetched_at": first,
        "source": "/".join(sorted(s for s in src if s)) or "unknown",
        "coverage": f"{len(inst)}/{len(universe)}",
        "text": (f"籌碼面:{d} 資料 · {first or '時間未記錄'} 取得 · "
                 f"來源 {'/'.join(sorted(s for s in src if s)) or '未標'} · "
                 f"{len(inst)}/{len(universe)} 檔"),
    }


def _purpose(stage, n, d, chips, with_chips) -> str:
    if stage == "盤中定案":
        if n == 0:
            return f"盤中定案({d} 13:20)— 無標的通過,籌碼面尚未納入"
        return f"盤中定案({d} 13:20)— {n} 檔待籌碼驗證,非進場名單"
    if n == 0:
        return f"明日清單({d})— 今日無符合標的"
    return f"明日清單({d})— {n} 檔,已通過籌碼驗證"


def summary_text(res: dict) -> str:
    """名單頂端的資料完整度宣告。"""
    lines = []
    for L in res.get("layers", []):
        lines.append(f"{L['layer']}: {L['entered']} → {L['survived']}")
    lines.append(res.get("chips", {}).get("text", ""))
    if res.get("count") == 0:
        lines.append("結論:今日無符合標的")
    for w in res.get("warnings", []):
        lines.append(f"⚠ {w}")
    return "\n".join(x for x in lines if x)
