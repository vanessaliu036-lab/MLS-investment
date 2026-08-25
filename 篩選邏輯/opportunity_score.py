"""Opportunity Scoring —— frozen signal 的每日 production 計分。

⚠ 這一層**不做任何 discovery、不擬合任何模型**。只做兩件事:
  1. 套用已凍結的訊號定義,算出今天每一檔的狀態
  2. 由該檔自己的近期實現結果,算出六項原始指標

凍結內容(winning_model_backtest/FROZEN_OPPORTUNITY_TARGET_V1.md):
  訊號   sec_rs_10d @ 族群中位數排名 Top10%(leave-one-out)
  Target Net MFE >= +3%,T+1 開盤進場,成本 47.1bps
  Horizon T+10 主、T+15 次
  母體   固定 51 檔,排除當下 EXTENDED,族群 LOO 同儕 >= 3

證據等級(必須顯示在 UI,不得包裝成買進推薦):
  歷史研究層 REPLICATED(2020-23 獨立窗 +4.71pp CI[+1.59,+8.48]、四年全正)
  Production 層 PENDING LIVE(2026-08-24 起才開始累積真正的 forward data)

⚠ 四層分級的核心規則:**55% Net Positive Rate 是主榜資格線,不是刪除線。**
   51 檔全部保留計算,勝率低但 payoff 結構好的一律進 HIGH_POTENTIAL,
   禁止因單一勝率門檻丟掉高 payoff 股票。
"""
from __future__ import annotations
import datetime as _dt
from typing import Optional

COST = 0.00471                 # 47.1bps 來回,與研究端一致
OPPORTUNITY_THRESHOLD = 0.03   # +3%
SECTOR_TOP_PCT = 0.90          # Top10%
MIN_SECTOR_PEERS = 3
TRAILING_WINDOW = 250          # 個股自身統計的回看交易日數(約一年)
MIN_TRAILING_N = 60            # unconditional 統計的最低樣本數
MIN_CONDITIONAL_N = 20         # conditional 統計低於此只能視為 DESCRIPTIVE_ONLY

# 四層分級門檻(見 CLAUDE.md Research Lead 章程第 9 條)
PRIMARY_POSITIVE_RATE = 55.0
HP_PF = 1.8
HP_WIN_LOSS = 2.0
HP_EXPECTED_UPSIDE = 5.0
HP_HIT_RATE = 65.0             # P(+3%) 高於獨立窗 in_top10 水準

VERSION = "opportunity_score_v1_2026-08-24"
EVIDENCE_LEVEL = "REPLICATED — PENDING LIVE"

# ── 條件參考值(**僅供 debug/對照,不得參與 ranking**)──────────────
# 來源:2020-07~2023-12 獨立窗(FROZEN_OPPORTUNITY_TARGET_V1.md 保守估計)。
# ⚠ 2026-08-24 定案:這組值對所有股票相同,拿它分層會**製造假的個股差異**
#    —— UI 看起來像有個股分層,實際上沒有。因此:
#      · 個股歷史不足時,六項指標一律標 None + INSUFFICIENT_HISTORY
#      · 該股票不參與 PRIMARY / HIGH_POTENTIAL 的個股層排序
#      · 本常數只放在 reference 欄位供對照,不進 ranking
CONDITIONAL_REFERENCE = {
    True: {   # 族群 Top10%
        "p_hit_3pct": 63.30, "expected_upside": 6.86, "expected_downside": -6.45,
        "net_positive_rate": 46.83, "profit_factor": 1.044, "net_expectancy": 0.140,
        "avg_win": 7.08, "avg_loss": -5.97, "mfe_given_hit": 10.23,
    },
    False: {  # 其餘
        "p_hit_3pct": 57.64, "expected_upside": 5.96, "expected_downside": -5.91,
        "net_positive_rate": 47.03, "profit_factor": 1.002, "net_expectancy": 0.005,
        "avg_win": 6.32, "avg_loss": -5.60, "mfe_given_hit": 9.55,
    },
}


def _signed_streak_free_mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def sector_rs_10d(sector_bars: dict[str, list], code: str, idx: int) -> Optional[float]:
    """族群 10 日相對強度(leave-one-out)。

    sector_bars: {code: [依日期排序的 close]},idx 為當日在序列中的位置。
    LOO 的理由:若把個股自己算進所屬族群強度,「強勢族群」就部分等於
    「這檔自己很強」,量到的是個股動能不是族群輪動。
    """
    def ret10(seq):
        if idx < 10 or seq[idx] is None or seq[idx - 10] in (None, 0):
            return None
        return seq[idx] / seq[idx - 10] - 1

    peers = [c for c in sector_bars if c != code]
    if len(peers) < MIN_SECTOR_PEERS:
        return None
    vals = [ret10(sector_bars[c]) for c in peers]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def realized_opportunity_stats(bars: list[dict], horizon: int = 10,
                               window: int = TRAILING_WINDOW,
                               signal_days: Optional[set] = None) -> dict:
    """由個股近 `window` 個交易日的**已實現**結果,算六項原始指標。

    ⚠ conditioning 規則必須顯式回報,因為它決定這些數字能不能用來分層:
      signal_days=None → **unconditional**:統計該檔所有歷史日。
        這等同「這檔過去表現好不好」= Static Stock Prior,而該假說已被
        walk-forward 驗證否決(過去強→未來強不成立)。
        **因此 unconditional 數字一律 DISPLAY_ONLY,不得參與 tier 決策。**
      signal_days=set(...) → **conditional**:只統計 frozen signal 觸發當日。
        這才是「訊號觸發時這檔的機會結構」,可作 descriptive differentiation,
        但必須同時揭露 n,n < MIN_CONDITIONAL_N 只能視為 descriptive。

    只用已成熟的樣本:進場日 + horizon 個交易日必須全部走完。
    回傳 outcome_matured_through = 最後一個成熟樣本的進場基準日。
    """
    n = len(bars)
    hits, mfes, maes, terms = [], [], [], []
    # 只取「進場日 + horizon 已完成」的樣本 —— 未成熟樣本絕不進統計
    last_complete = n - horizon - 1
    start = max(0, last_complete - window + 1)
    matured_through = None
    for i in range(start, last_complete + 1):
        bar_date = bars[i].get("data_date") or bars[i].get("date")
        if signal_days is not None and bar_date not in signal_days:
            continue
        entry = bars[i + 1].get("open")
        if not entry:
            continue
        win = bars[i + 1:i + 1 + horizon]
        highs = [b.get("high") for b in win if b.get("high") is not None]
        lows = [b.get("low") for b in win if b.get("low") is not None]
        if not highs or not lows or len(win) < horizon:
            continue
        mfe = max(highs) / entry - 1 - COST
        mae = min(lows) / entry - 1 - COST
        close_h = win[-1].get("close")
        if close_h is None:
            continue
        term = close_h / entry - 1 - COST
        hits.append(mfe >= OPPORTUNITY_THRESHOLD)
        mfes.append(mfe); maes.append(mae); terms.append(term)
        matured_through = bar_date

    conditioning = "conditional_on_frozen_signal" if signal_days is not None else "unconditional"
    min_n = MIN_CONDITIONAL_N if signal_days is not None else MIN_TRAILING_N
    if len(hits) < min_n:
        return {"n": len(hits), "insufficient": True, "conditioning": conditioning,
                "horizon": horizon, "outcome_matured_through": matured_through}

    wins = [t for t in terms if t > 0]
    losses = [t for t in terms if t <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None
    hit_mfes = [m for m, h in zip(mfes, hits) if h]
    return {
        "n": len(hits),
        "insufficient": False,
        "conditioning": conditioning,
        "horizon": horizon,
        "outcome_matured_through": matured_through,
        # ── 六項原始指標(章程第 10 條:必須全部保留在資料層)──
        "p_hit_3pct": round(sum(hits) / len(hits) * 100, 2),
        "expected_upside": round(sum(mfes) / len(mfes) * 100, 2),
        "expected_downside": round(sum(maes) / len(maes) * 100, 2),
        "net_positive_rate": round(len(wins) / len(terms) * 100, 2),
        "profit_factor": round(pf, 3) if pf else None,
        "net_expectancy": round(sum(terms) / len(terms) * 100, 3),
        # ── 分層需要的補充 ──
        "avg_win": round(sum(wins) / len(wins) * 100, 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses) * 100, 2) if losses else None,
        "mfe_given_hit": round(sum(hit_mfes) / len(hit_mfes) * 100, 2) if hit_mfes else None,
    }


def assign_tier(sector_opportunity: bool, cond_stats: dict,
                excluded: bool = False) -> tuple[str, list[str]]:
    """四層分級。

    ⚠ 2026-08-24 定案的關鍵界線:
      **unconditional per-stock 統計不得參與分層。**
      「這檔過去勝率/PF 比較高 → 下一期仍比較強」= Static Stock Prior,
      已被 walk-forward 驗證否決(見專案記憶 pa-stock-sector-prior-no-persistence)。
      拿全歷史 Win Rate / PF 在族群內排序,等於把已否決的假說從側門放回來。

    目前唯一有 replicated evidence 的是:
      sec_rs_10d @ Top10% → 未來 T+10/T+15 出現 Net MFE >= +3% 的機會提高
    **不是**「2383 歷史 PF 3.816 所以比 1815 更值得買」。

    因此分層只能用:
      · frozen sector signal(已 replicated)
      · Pre-Activation EXTENDED(禁追,獨立驗證過的排除條件)
      · conditional 統計(只在訊號觸發日的樣本)—— 且 n >= MIN_CONDITIONAL_N
        才可用於區分 PRIMARY;n 不足時只能 descriptive,不得升級。
    """
    if excluded:
        return "AVOID", ["Pre-Activation EXTENDED(已漲太多,不追)"]

    if not sector_opportunity:
        return "WATCH", ["Sector Opportunity = FALSE(sec_rs_10d 未進族群 Top10%)"]

    reasons = ["Sector Opportunity = TRUE(sec_rs_10d 族群 Top10%,歷史已 replicated)"]

    n = cond_stats.get("n", 0)
    if cond_stats.get("insufficient") or n < MIN_CONDITIONAL_N:
        reasons.append(
            f"Stock-level differentiation = NOT YET AVAILABLE"
            f"(訊號觸發日樣本 n={n} < {MIN_CONDITIONAL_N},DESCRIPTIVE_ONLY)")
        return "HIGH_POTENTIAL", reasons

    # conditional 樣本足夠 → 才允許用它區分 PRIMARY
    pos = cond_stats.get("net_positive_rate") or 0
    pf = cond_stats.get("profit_factor") or 0
    up = cond_stats.get("expected_upside") or 0
    hit = cond_stats.get("p_hit_3pct") or 0
    aw, al = cond_stats.get("avg_win"), cond_stats.get("avg_loss")
    wl = (aw / abs(al)) if (aw and al and al != 0) else 0

    # ⚠ conditional 統計可以做 descriptive differentiation,但它本身
    #   **尚未通過 walk-forward / max-stat / 獨立窗**,不是已驗證的個股層 edge。
    #   唯一 replicated 的仍是族群層訊號。這行必須跟著 PRIMARY 一起出現。
    caveat = (f"⚠ 個股層區分為 DESCRIPTIVE_ONLY(n={n},未經 walk-forward/"
              f"max-stat/獨立窗驗證),不得解讀為已驗證的個股選股 edge")
    if pos >= PRIMARY_POSITIVE_RATE:
        reasons.append(f"訊號觸發日勝率 {pos:.1f}% 達主榜線(n={n})")
        reasons.append(caveat)
        return "PRIMARY", reasons

    hp = []
    if hit >= HP_HIT_RATE:
        hp.append(f"P(+3%)={hit:.1f}%")
    if pf >= HP_PF:
        hp.append(f"PF={pf:.2f}")
    if wl >= HP_WIN_LOSS:
        hp.append(f"賺賠比={wl:.2f}")
    if up >= HP_EXPECTED_UPSIDE:
        hp.append(f"ExpUpside={up:.1f}%")
    if hp:
        reasons.append(f"勝率 {pos:.1f}% 未達主榜線,但訊號觸發日 payoff 結構強:"
                       + "、".join(hp) + f"(n={n})")
    else:
        reasons.append(f"訊號觸發日勝率 {pos:.1f}%、無突出 payoff 特徵(n={n})")
    reasons.append(caveat)
    return "HIGH_POTENTIAL", reasons


def score_one(code: str, bars: list[dict], sector_bars: dict[str, list],
              sector_rank_pct: Optional[float], stage: Optional[str],
              signal_days: Optional[set] = None,
              audit: Optional[dict] = None) -> dict:
    """單檔的完整 production 計分。

    回傳兩套統計,用途嚴格分開:
      display_stats     unconditional(全歷史)—— **DISPLAY_ONLY,不參與分層**
      conditional_stats 只在 frozen signal 觸發日 —— 可用於分層,但須 n 足夠
    """
    in_top = (sector_rank_pct is not None and sector_rank_pct > SECTOR_TOP_PCT)
    excluded = (stage == "EXTENDED")

    disp10 = realized_opportunity_stats(bars, 10)
    disp15 = realized_opportunity_stats(bars, 15)
    cond10 = realized_opportunity_stats(bars, 10, signal_days=signal_days or set())
    cond15 = realized_opportunity_stats(bars, 15, signal_days=signal_days or set())
    for d in (disp10, disp15):
        d["usage"] = "DISPLAY_ONLY"
        d["stats_basis"] = "INSUFFICIENT_HISTORY" if d.get("insufficient") else "per_stock_unconditional"
    for d in (cond10, cond15):
        d["usage"] = ("TIERING" if not d.get("insufficient") else "DESCRIPTIVE_ONLY")
        d["stats_basis"] = "per_stock_conditional_on_signal"

    tier, reasons = assign_tier(in_top, cond10, excluded)
    return {
        "code": code,
        "signal_in_top_sector": bool(in_top),
        "sector_opportunity": bool(in_top),
        "sector_rank_pct": (round(sector_rank_pct, 4)
                            if sector_rank_pct is not None else None),
        "pa_stage": stage,
        "tier": tier,
        "tier_reasons": reasons,
        "evidence_level": EVIDENCE_LEVEL,
        # 兩層證據等級必須分開 —— 族群層已 replicated,個股層尚未驗證
        "sector_level_evidence": "REPLICATED (2020-23 independent window)",
        "stock_level_evidence": (
            f"DESCRIPTIVE_ONLY (n={cond10.get('n', 0)}, not validated)"
            if not cond10.get("insufficient")
            else "NOT YET AVAILABLE (insufficient conditional samples)"),
        # ⚠ 展示用(unconditional)—— UI 可顯示,但不得暗示它決定了分層
        "display_stats_t10": disp10,
        "display_stats_t15": disp15,
        # 分層依據(conditional on frozen signal)
        "conditional_stats_t10": cond10,
        "conditional_stats_t15": cond15,
        "stock_level_available": (not cond10.get("insufficient", True)),
        "conditional_reference": CONDITIONAL_REFERENCE[bool(in_top)],
        # ── 不可變稽核欄位(snapshot 寫入後不得回頭重算)──
        **(audit or {}),
        "score_version": VERSION,
    }
