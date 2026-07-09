"""
MLS 標準版 — backtest.py
歷史回放預訓練:用 Shioaji 日K回放近 N 個交易日,
把五因子的觸發→隔日成敗寫入 factor_stats,預先算出:
  · 因子權重(寫入 factor_weights,開盤自動載入)
  · 進場分數門檻 entry_score_min(朝 80% 精度目標校準)
部署第一天就帶著訓練過的參數上線,不用冷啟動。

用法:
  python backtest.py            # 預設回放 60 個交易日
  python backtest.py 120        # 回放 120 日

【誠實聲明】本回測用「日K近似」盤中訊號:
  趨勢=收盤>前日MA5且創5日新高;量能=當日量/前5日均量;
  RS=個股漲幅−族群中位;籌碼用當前快取(近月值近似歷史)。
  它校準的是因子相對權重與門檻,不是逐筆重現盤中;
  上線後每日盤後學習會持續用真實訊號覆蓋修正。
"""

import sys
import json
from statistics import median

import config as C
import broker
import chips
import db
import scoring


def load_history(days):
    """回傳 {code: [ {close, high, volume}, ... 舊→新 ]}(每檔 days+8 根)。"""
    hist = {}
    for code in C.UNIVERSE:
        kb = broker.daily_kbars(code, days=days + 8)
        if kb and len(kb) >= 10:
            hist[code] = kb
    return hist


def run(days=60, target_precision=0.80):
    db.init()
    hist = load_history(days)
    if not hist:
        print("無歷史資料,確認 Shioaji 連線")
        return

    n_days = min(len(v) for v in hist.values()) - 6
    print(f"回放 {len(hist)} 檔 × {n_days - 1} 日")

    # 逐日模擬:t 日訊號 → t+1 日成敗
    samples = []          # (factors, score_raw, success)
    for t in range(5, n_days - 1):
        # 族群中位(當日)
        day_chg = {}
        for code, kb in hist.items():
            prev, cur = kb[t - 1]["close"], kb[t]["close"]
            if prev:
                day_chg[code] = (cur - prev) / prev * 100
        sec_chgs = {}
        for code, chg in day_chg.items():
            sec = C.SECTOR_MAP.get(code, ("其他",))[0]
            sec_chgs.setdefault(sec, []).append(chg)
        sec_med = {k: median(v) for k, v in sec_chgs.items()}
        mkt_med = median(day_chg.values()) if day_chg else 0

        for code, kb in hist.items():
            if code in getattr(C, "ENGINE_STOCKS", set()):
                continue
            chg = day_chg.get(code)
            if chg is None or chg < 0.5:      # 只評估翻紅日(近似盤中候選)
                continue
            closes = [k["close"] for k in kb[t - 5:t]]
            ma5 = sum(closes) / 5
            hi5 = max(k["high"] for k in kb[t - 5:t])
            vols = [k["volume"] for k in kb[t - 5:t] if k.get("volume")]
            avg5v = sum(vols) / len(vols) if vols else None
            vr = (kb[t].get("volume") or 0) / avg5v if avg5v else None

            F = {"trend": 0, "volume": 0, "rs": 0, "chip": 0, "sector": 0}
            if kb[t]["close"] > ma5:            F["trend"] += 10
            if kb[t]["close"] > hi5:            F["trend"] += 10
            if vr is not None:
                if vr >= 2.0:   F["volume"] = 25
                elif vr >= 1.5: F["volume"] = 18
                elif vr >= 1.2: F["volume"] = 10
            sec = C.SECTOR_MAP.get(code, ("其他",))[0]
            rs = chg - sec_med.get(sec, 0)
            if rs > 1: F["rs"] += 12
            elif rs > 0: F["rs"] += 6
            if chg - mkt_med > 0: F["rs"] += 8
            ch = chips.get_chips(code)
            if (ch.get("inst_net_20d_lots") or 0) > 0: F["chip"] += 10
            if (ch.get("inst_streak") or 0) >= 3:      F["chip"] += 5
            if (ch.get("big_holder_trend") or 0) > 0:  F["chip"] += 5
            if sec_med.get(sec, 0) > 1.0:              F["sector"] += 8

            raw = sum(F.values())
            nxt = hist[code][t + 1]["close"]
            success = nxt > kb[t]["close"] * 1.003
            samples.append((F, raw, success))

    if not samples:
        print("樣本不足")
        return

    # ① 因子命中率 → 權重
    FACTOR_HIT = {"trend": 10, "volume": 10, "rs": 8, "chip": 10, "sector": 8}
    stats = {f: [0, 0] for f in FACTOR_HIT}
    for F, raw, ok in samples:
        for f, thr in FACTOR_HIT.items():
            if F[f] >= thr:
                stats[f][0] += 1
                stats[f][1] += 1 if ok else 0
    rows = [{"factor": f, "triggered": t_, "success": s_}
            for f, (t_, s_) in stats.items() if t_]
    db.record_factor_stats(rows)
    weights = db.update_factor_weights(days=days + 5)

    # ② 門檻校準:找最低 score 門檻使精度 ≥ 目標(樣本≥30)
    best_thr = 40
    for thr in range(35, 71):
        hit = [ok for F, raw, ok in samples if raw >= thr]
        if len(hit) >= 30 and sum(hit) / len(hit) >= target_precision:
            best_thr = thr
            break
    else:
        # 達不到目標精度 → 取精度最高的門檻(誠實回報)
        best = (40, 0)
        for thr in range(35, 71):
            hit = [ok for F, raw, ok in samples if raw >= thr]
            if len(hit) >= 30:
                p = sum(hit) / len(hit)
                if p > best[1]:
                    best = (thr, p)
        best_thr = best[0]
        print(f"⚠️ 回測期間任何門檻都達不到 {target_precision:.0%};"
              f"最佳門檻 {best[0]}(精度 {best[1]:.1%})。已採用,盤後學習會續調。")
    db.kv_set("entry_score_min", best_thr)

    sel = [ok for F, raw, ok in samples if raw >= best_thr]
    print(f"樣本 {len(samples)} 筆|門檻 {best_thr}|"
          f"入選 {len(sel)} 筆|精度 {sum(sel)/len(sel):.1%}" if sel else "無入選樣本")
    print("因子權重:", json.dumps(weights, ensure_ascii=False))
    print("✓ 權重與門檻已寫入 mls.db,server.py 啟動即載入")


if __name__ == "__main__":
    d = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    run(days=d)
