"""
MLS 模組 — funnel.py(明日觀察四關漏斗,正確三態判定版)
====================================================================
修正使用者抓到的真 bug:舊實作把「無資料」偷偷當成「通過」,
用資料缺失湊出過關數 → 台勝科市場面無資料、技術面不合格、籌碼面未取得,
卻被判「過三關差一關」。這違反鐵律「缺資料就明講、不假造」。

核心設計:每一關回傳三態,絕不二態——
  PASS     有資料 且 達標
  FAIL     有資料 但 未達標
  NO_DATA  根本沒有資料(不能算過、也不能算差一關)

梯隊規則(嚴格):
  第一梯隊(🔴 四關全過) = 四關全部 PASS
  第二梯隊(🟡 差一關)   = 三關 PASS 且 剩一關 FAIL(不是 NO_DATA)
  資料不全(⚪ 待補)     = 任何一關是 NO_DATA → 踢出梯隊,標「資料不全」
  淘汰                    = 兩關以上 FAIL,或過關數不足

鐵律:NO_DATA 永遠不算 PASS。有任何 NO_DATA 就不可能進第一/第二梯隊。
     寧可顯示「資料不全·待補」,不讓殘缺資料偽裝成「差一關的好標的」。

四關順序(使用者定案):市場 → 技術 → 資金 → 抗跌
資料來源接上前,市場/籌碼多為 NO_DATA 是正常的 → 這些股票會落在
「資料不全」區,而不是被美化成第二梯隊。這正是誠實的行為。
"""

from enum import Enum


class Gate(str, Enum):
    PASS = "PASS"        # 有資料且達標
    FAIL = "FAIL"        # 有資料但未達標
    NO_DATA = "NO_DATA"  # 無資料,不可算過


class Tier(str, Enum):
    T1 = "第一梯隊"          # 四關全過
    T2 = "第二梯隊"          # 過三關差一關(差的是 FAIL,不是 NO_DATA)
    INCOMPLETE = "資料不全"  # 有任一關 NO_DATA
    OUT = "淘汰"


# ════════════════════════════════════════════════════════
# 四關判定(每關回傳 (Gate, 一句人話證據))
# 每關的輸入都可能是 None = 該關資料源還沒接上 → NO_DATA
# ════════════════════════════════════════════════════════
def gate_market(sector_pct, is_weak_sector, market_data_ok):
    """市場面:官方三大法人+大盤+族群強弱。官方資料沒到 = NO_DATA。"""
    if not market_data_ok or sector_pct is None:
        return Gate.NO_DATA, "官方市場資料未取得(大盤/三大法人)"
    if is_weak_sector:
        return Gate.FAIL, f"族群 {sector_pct:+.1f}% · 弱勢族群"
    return Gate.PASS, f"族群 {sector_pct:+.1f}% · 非弱勢"


def gate_tech(livermore_qualified):
    """技術面:李佛摩六點。qualified None=沒算到資料;False=算了但不合格。"""
    if livermore_qualified is None:
        return Gate.NO_DATA, "技術資料未取得(日K/六點未計算)"
    if livermore_qualified == "long":
        return Gate.PASS, "李佛摩六點合格(多方)"
    return Gate.FAIL, "李佛摩六點未合格"


def gate_money(health_quadrant, flow_streak, aflow_alive):
    """資金面:四象限 + 資金連續性。象限 None=沒資料。
    aflow_alive=False 表示主動買賣量未餵入(資金腳降級),此時
    只能用象限近似,無法確認資金方向 → 若象限本身也弱則 FAIL,
    但象限完全沒有 = NO_DATA。"""
    if health_quadrant is None:
        return Gate.NO_DATA, "資金象限未取得"
    healthy = health_quadrant in ("in_up", "in_down")  # 資金流入類
    streak_txt = f",資金連續{flow_streak}日" if flow_streak else ""
    degrade = "" if aflow_alive else "(主動買賣量未餵入,僅象限近似)"
    if health_quadrant == "in_up":
        return Gate.PASS, f"流入且漲,健康{streak_txt}{degrade}"
    if health_quadrant == "in_down":
        # 流入但跌:假紅待驗證,算 FAIL(不是無資料)
        return Gate.FAIL, f"流入但跌,假紅待驗證{streak_txt}{degrade}"
    return Gate.FAIL, f"資金流出({health_quadrant}){degrade}"


def gate_resilience(passed_screener, chip_alignment):
    """抗跌/假設面:抗跌篩選器 + 籌碼共振。
    passed_screener None=篩選器沒跑到;chip_alignment None=籌碼未取得。
    籌碼共振是這關的核心證據,未取得 → NO_DATA(不能假裝過)。"""
    if chip_alignment is None:
        return Gate.NO_DATA, "籌碼共振未取得(短/中/長線資料不全)"
    if passed_screener is None:
        return Gate.NO_DATA, "抗跌篩選未計算"
    # chip_alignment: 'green'(三層一致) / 'yellow'(長中一致) / 'red'(不一致)
    if passed_screener and chip_alignment in ("green", "yellow"):
        return Gate.PASS, f"抗跌成立 · 籌碼共振{'🟢三層一致' if chip_alignment=='green' else '🟡長中一致'}"
    if chip_alignment == "red":
        return Gate.FAIL, "籌碼共振🔴三層不一致"
    return Gate.FAIL, "抗跌不成立"


# ════════════════════════════════════════════════════════
# 梯隊裁定(嚴格三態,NO_DATA 一律踢出)
# ════════════════════════════════════════════════════════
def classify(gates):
    """
    gates: [(Gate, reason), ...] 四關結果,順序=市場/技術/資金/抗跌。
    回傳 (Tier, 差的關名 or None, 說明)。
    鐵律:任一 NO_DATA → INCOMPLETE(不進任何梯隊)。
    """
    names = ["市場面", "技術面", "資金面", "抗跌面"]
    states = [g[0] for g in gates]

    n_nodata = states.count(Gate.NO_DATA)
    n_pass = states.count(Gate.PASS)
    n_fail = states.count(Gate.FAIL)

    # 鐵律1:有任何一關無資料 → 資料不全,不進梯隊,不把 NO_DATA 算成過
    if n_nodata > 0:
        missing = [names[i] for i, s in enumerate(states) if s == Gate.NO_DATA]
        return Tier.INCOMPLETE, None, f"資料不全·待補({'/'.join(missing)})"

    # 到這裡:四關全部有資料(非 NO_DATA)
    if n_pass == 4:
        return Tier.T1, None, "四關全過"
    if n_pass == 3 and n_fail == 1:
        diff = names[states.index(Gate.FAIL)]
        return Tier.T2, diff, f"過三關,差【{diff}】"
    return Tier.OUT, None, f"淘汰(過{n_pass}關/不過{n_fail}關)"


def evaluate(stock):
    """
    單檔完整評估。stock 需含各關所需欄位;缺的傳 None 會自然落入 NO_DATA。
    回傳 dict:tier, diff_gate, summary, gates(四關明細含人話證據)。
    """
    gates = [
        gate_market(stock.get("sector_pct"), stock.get("is_weak_sector"),
                    stock.get("market_data_ok", False)),
        gate_tech(stock.get("livermore_qualified")),
        gate_money(stock.get("health_quadrant"), stock.get("flow_streak"),
                   stock.get("aflow_alive", False)),
        gate_resilience(stock.get("passed_screener"),
                        stock.get("chip_alignment")),
    ]
    tier, diff, summary = classify(gates)
    names = ["市場面", "技術面", "資金面", "抗跌面"]
    return {
        "code": stock.get("code"), "name": stock.get("name"),
        "tier": tier.value, "diff_gate": diff, "summary": summary,
        "gates": [{"name": names[i], "state": g[0].value, "evidence": g[1]}
                  for i, g in enumerate(gates)],
    }


def build_funnel(stocks):
    """全池評估,回傳三組:第一梯隊 / 第二梯隊 / 資料不全(淘汰不回傳)。"""
    t1, t2, incomplete = [], [], []
    for s in stocks:
        r = evaluate(s)
        if r["tier"] == Tier.T1.value:
            t1.append(r)
        elif r["tier"] == Tier.T2.value:
            t2.append(r)
        elif r["tier"] == Tier.INCOMPLETE.value:
            incomplete.append(r)
        # OUT 淘汰不顯示
    return {"tier1": t1, "tier2": t2, "incomplete": incomplete}


# ════════════════════════════════════════════════════════
# 離線驗證:重現台勝科矛盾案例 + 各種組合
# ════════════════════════════════════════════════════════
if __name__ == "__main__":
    # ① 台勝科實況:市場無資料、技術不合格、資金過、籌碼未取得
    #    舊 bug 會判「過三關差一關」;正確應為「資料不全」
    tai = evaluate({
        "code": "3532", "name": "台勝科",
        "sector_pct": -1.6, "is_weak_sector": False, "market_data_ok": False,  # 市場 NO_DATA
        "livermore_qualified": False,        # 技術 FAIL
        "health_quadrant": "in_up", "flow_streak": 2, "aflow_alive": False,  # 資金 PASS(降級)
        "chip_alignment": None,              # 抗跌 NO_DATA
    })
    print("① 台勝科:", tai["tier"], "—", tai["summary"])
    for g in tai["gates"]:
        print(f"    {g['name']}: {g['state']:8} {g['evidence']}")
    assert tai["tier"] == "資料不全", "有 NO_DATA 就不能進梯隊!"
    print("   ✅ 正確判為『資料不全』,不再把無資料當過關\n")

    # ② 真正的第二梯隊:四關都有資料,只有技術 FAIL
    t2 = evaluate({
        "code": "5483", "name": "中美晶",
        "sector_pct": 2.0, "is_weak_sector": False, "market_data_ok": True,   # 市場 PASS
        "livermore_qualified": False,        # 技術 FAIL
        "health_quadrant": "in_up", "flow_streak": 3, "aflow_alive": True,    # 資金 PASS
        "passed_screener": True, "chip_alignment": "green",                    # 抗跌 PASS
    })
    print("② 中美晶(四關有資料,僅技術不合格):", t2["tier"], "—", t2["summary"])
    assert t2["tier"] == "第二梯隊" and t2["diff_gate"] == "技術面"
    print("   ✅ 這才是真正的『差一關』\n")

    # ③ 第一梯隊:四關全過
    t1 = evaluate({
        "code": "6182", "name": "合晶",
        "sector_pct": 3.0, "is_weak_sector": False, "market_data_ok": True,
        "livermore_qualified": "long",
        "health_quadrant": "in_up", "flow_streak": 3, "aflow_alive": True,
        "passed_screener": True, "chip_alignment": "green",
    })
    print("③ 合晶(四關全過):", t1["tier"], "—", t1["summary"])
    assert t1["tier"] == "第一梯隊"
    print("   ✅ Strong Ready\n")

    # ④ 淘汰:兩關 FAIL
    out = evaluate({
        "code": "9999", "name": "測試弱股",
        "sector_pct": -5.0, "is_weak_sector": True, "market_data_ok": True,   # 市場 FAIL
        "livermore_qualified": False,        # 技術 FAIL
        "health_quadrant": "in_up", "flow_streak": 1, "aflow_alive": True,    # 資金 PASS
        "passed_screener": True, "chip_alignment": "green",                    # 抗跌 PASS
    })
    print("④ 弱股(市場+技術兩關 FAIL):", out["tier"], "—", out["summary"])
    assert out["tier"] == "淘汰"
    print("   ✅ 淘汰,不顯示\n")

    print("—— 四關漏斗三態判定全部驗證通過 ——")
    print("鐵律確認:NO_DATA 永遠不算過關,有缺資料一律『資料不全』踢出梯隊")
