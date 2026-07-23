"""MLS v4.0 四層漏斗：資金粗篩、籌碼技術確認、融資反向排雷、玩法分流。"""
import config as C

PASS, FAIL, NO_DATA = "PASS", "FAIL", "NO_DATA"


def gate1_flow(ev):
    """L1：只保留資金流入且健康分達可觀察門檻的個股。"""
    if ev.get("quad") not in ("in_up", "in_down"):
        return FAIL
    score = ev.get("score")
    return PASS if score is not None and score >= C.L1_HEALTH_MIN else FAIL


def gate2_chip_technical(ev):
    """L2：20日法人為正＋5日連續性、站上MA20、承接至少3星。"""
    net = ev.get("inst_net_20d")
    inst5 = ev.get("inst_5d_net")
    trust5 = ev.get("trust_5d_net")
    if net is None or inst5 is None or trust5 is None:
        return NO_DATA
    if net <= 0 or not (inst5 > 0 or trust5 > 0):
        return FAIL
    if not ev.get("above_ma20"):
        return FAIL
    if ev.get("stars", 0) < C.ABSORPTION_GATE_MIN:
        return FAIL
    return PASS


def gate2_inst(ev):
    """相容舊呼叫名稱。"""
    return gate2_chip_technical(ev)


def apply_l3_margin_rule(ev):
    """L3：融資下降加分；融資暴增且噴漲時 Ready 封頂 Watch。"""
    margin = ev.get("margin_5d_chg")
    if margin is None:
        return NO_DATA
    if margin > C.MARGIN_SURGE_TH and (ev.get("chg", 0) >= 7 or ev.get("near_limit", False)):
        ev["grade"] = "Watch"
        ev["margin_risk"] = "融資暴增且噴漲，Ready封頂Watch"
    elif margin < 0:
        ev["score"] = min(100, (ev.get("score") or 0) + C.WASH_BONUS)
        ev["margin_bonus"] = "融資下降·洗籌加分"
    return PASS


def gate3_margin(ev):
    """L3 是排雷/標記，不把融資下降誤當成必須條件。"""
    return apply_l3_margin_rule(ev)


def gate4_livermore(ev):
    """L4：李佛摩只分流玩法，不篩掉個股。"""
    return PASS


def run_funnel(evals):
    universe = list(evals)
    stage_results = []

    def apply_gate(items, gate_fn, name):
        kept, detail = [], []
        for ev in items:
            result = gate_fn(ev)
            detail.append({"code": ev["code"], "result": result})
            if result == PASS:
                kept.append(ev)
        stage_results.append({"gate": name, "in": len(items), "out": len(kept), "detail": detail})
        return kept

    s1 = apply_gate(universe, gate1_flow, "L1 資金健康度粗篩")
    s2 = apply_gate(s1, gate2_chip_technical, "L2 籌碼技術確認")
    # L3 不篩選：只對通過 L2 的標的加分或降級。
    for ev in s2:
        apply_l3_margin_rule(ev)
    stage_results.append({"gate": "L3 融資反向排雷", "in": len(s2), "out": len(s2),
                          "detail": [{"code": e["code"], "result": PASS,
                                      "risk": e.get("margin_risk"), "bonus": e.get("margin_bonus")}
                                     for e in s2]})
    # vs族群只排序，同族群相對領先者優先；L4不參與資格判定。
    s2.sort(key=lambda e: (e.get("vs_sector") or 0), reverse=True)
    stage_results.append({"gate": "L4 李佛摩玩法分流", "in": len(s2), "out": len(s2),
                          "detail": [{"code": e["code"], "result": PASS,
                                      "track": e.get("track")} for e in s2]})
    return {"universe": len(universe), "stages": stage_results, "passed": s2,
            "passed_codes": [e["code"] for e in s2]}


def gate_status_for(ev):
    return {"flow": gate1_flow(ev), "chip_technical": gate2_chip_technical(ev),
            "margin": PASS, "livermore": PASS}
