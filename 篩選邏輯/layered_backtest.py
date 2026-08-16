# -*- coding: utf-8 -*-
"""layered_backtest.py — 新版雙分數分層 scorer 的多日回測/量測(唯讀)。

自動掃 daily_bar 裡所有交易日:對每個有「隔一交易日收盤」的 signal 日,
用新版 scorer 分層,join T+1 實際漲跌,逐日 + 累積統計各 tier 鑑別度與誤刪率。
可每天重跑(新資料到就自動納入)= 量測側日累積。不寫任何表。

用法: python3 layered_backtest.py            # 全部可回測日
       python3 layered_backtest.py 2026-08-06 # 單一 signal 日
"""
import sys, sqlite3, statistics
from collections import defaultdict
sys.path.insert(0, "/opt/mls-screen")
import layered_score as L, config

DB = "/opt/mls-screen/mls.db"
MISKILL_RET = 3.0   # 淘汰檔 T+1 收盤漲幅 >= 此值 = 誤刪(對齊 reject_verify)
MIN_COVER = 20      # signal 日至少幾檔有 bar 才納入(避免稀疏日污染)
MIN_STD   = 0.10    # 退化日護欄:T+1 全檔報酬離散度 < 此值(pp)判為壞資料日,跳過不納入
LIMIT_PCT = 10.5    # 台股單日漲跌幅上限(含緩衝);T+1 |報酬|>此值 = 物理不可能 = 壞 bar
BAD_FRAC  = 0.20    # 一天內超過上限的個股比例 >= 此值 → 該日 daily_bar 判為污染,跳過

c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
CG = config.CODE_GROUP

def trading_days():
    return [r[0] for r in c.execute("SELECT DISTINCT data_date FROM daily_bar ORDER BY data_date")]

def rows(t, d):
    return {r["code"]: dict(r) for r in c.execute(f"SELECT * FROM {t} WHERE data_date=?", (d,))}

def recent(t, code, upto, n):
    return [dict(r) for r in c.execute(
        f"SELECT * FROM {t} WHERE code=? AND data_date<=? ORDER BY data_date DESC LIMIT ?",
        (code, upto, n))]

def deriv(code, sig):
    bb = recent("daily_bar", code, sig, 6); ii = recent("inst_flow", code, sig, 5)
    cl = [x["close"] for x in bb if x["close"] is not None]; cr = up = i3 = i5 = None
    if len(cl) >= 2 and cl[1]: cr = round((cl[0]-cl[1])/cl[1]*100, 2)
    if cl:
        up = 0
        for a, b in zip(cl, cl[1:]):
            if a > b: up += 1
            else: break
    nn = [x["total_net"] for x in ii if x["total_net"] is not None]
    if len(nn) >= 3: i3 = sum(nn[:3])
    if len(nn) >= 5: i5 = sum(nn[:5])
    return dict(change_rate=cr, up_days=up, inst_3d=i3, inst_5d=i5)

def score_day(sig):
    b = rows("daily_bar", sig); ins = rows("inst_flow", sig)
    if len(b) < MIN_COVER: return None
    chg = {code: deriv(code, sig)["change_rate"] for code in b}
    valid = [v for v in chg.values() if v is not None]
    mkt = statistics.median(valid) if valid else None
    sv = defaultdict(list)
    for code in b:
        s, v = CG.get(code), chg[code]
        if s and v is not None: sv[s].append(v)
    sm = {s: statistics.median(x) for s, x in sv.items() if x}
    out = {}
    for code in b:
        dv = deriv(code, sig); s, v = CG.get(code), dv["change_rate"]
        sr = round(v-sm[s], 2) if (v is not None and s in sm) else None
        mr = round(v-mkt, 2) if (v is not None and mkt is not None) else None
        r = L.score_layered(L.build_input(code, b.get(code), ins.get(code),
                                          sector_rel=sr, market_rel=mr, **dv))
        out[code] = r["tier"]
    return out

def t1_returns(sig, nxt):
    b0 = rows("daily_bar", sig); b1 = rows("daily_bar", nxt)
    return {code: round((b1[code]["close"]-b0[code]["close"])/b0[code]["close"]*100, 2)
            for code in b0 if code in b1 and b0[code]["close"]}

ORDER = [L.TIER_CORE, L.TIER_NO_CHASE, L.TIER_CANDIDATE, L.TIER_REJECTED]
days = trading_days()
arg = sys.argv[1] if len(sys.argv) > 1 else None
pairs = [(days[i], days[i+1]) for i in range(len(days)-1)]
if arg: pairs = [(s, n) for s, n in pairs if s == arg]

cum = defaultdict(list); miskill_hit = miskill_tot = 0
print("=== 新版分層 scorer 多日回測(signal→T+1 實際)===\n")
for sig, nxt in pairs:
    tiers = score_day(sig)
    if not tiers: continue
    rets = t1_returns(sig, nxt)
    _rv = list(rets.values())
    _bad = sum(1 for x in _rv if abs(x) > LIMIT_PCT)
    if len(_rv) >= 2 and statistics.pstdev(_rv) < MIN_STD:
        print(f"[{sig}\u2192{nxt}] \u8df3\u904e\uff1a\u9000\u5316\u65e5(T+1 \u5168\u6a94\u540c\u503c\uff0cstd={statistics.pstdev(_rv):.3f}pp)")
        continue
    if _rv and _bad / len(_rv) >= BAD_FRAC:
        print(f"[{sig}\u2192{nxt}] \u8df3\u904e\uff1a\u6c61\u67d3\u65e5({_bad}/{len(_rv)} \u6a94 |T+1|>{LIMIT_PCT}%\uff0c\u8d85\u6f32\u8dcc\u5e45\u4e0a\u9650=\u58de bar)")
        continue
    byt = defaultdict(list)
    for code, tier in tiers.items():
        if code in rets: byt[tier].append(rets[code])
    line = []
    for t in ORDER:
        rr = byt.get(t, [])
        if rr: line.append(f"{t[:2]} n{len(rr)} 均{statistics.mean(rr):+.1f}%")
    print(f"[{sig}→{nxt}] " + "  ".join(line))
    for t, rr in byt.items(): cum[t].extend(rr)
    for r in byt.get(L.TIER_REJECTED, []):
        miskill_tot += 1
        if r >= MISKILL_RET: miskill_hit += 1

print("\n=== 累積({} 個回測日)===".format(len(pairs)))
for t in ORDER:
    rr = cum.get(t, [])
    if rr:
        up = sum(1 for x in rr if x > 0)
        print(f"  {t:<12} n={len(rr):>3}  平均T+1={statistics.mean(rr):+.2f}%  中位={statistics.median(rr):+.2f}%  上漲率={round(up/len(rr)*100)}%")
if miskill_tot:
    print(f"\n  誤刪率(淘汰檔 T+1>=+{MISKILL_RET}%) = {miskill_hit}/{miskill_tot} = {round(miskill_hit/miskill_tot*100,1)}%")
# 鑑別度:核心+禁追 vs 淘汰 的平均差
keep = cum.get(L.TIER_CORE, []) + cum.get(L.TIER_NO_CHASE, [])
rej = cum.get(L.TIER_REJECTED, [])
if keep and rej:
    print(f"  鑑別度:保留(核心+禁追) 均{statistics.mean(keep):+.2f}%  vs  淘汰 均{statistics.mean(rej):+.2f}%  差={statistics.mean(keep)-statistics.mean(rej):+.2f}pp")
