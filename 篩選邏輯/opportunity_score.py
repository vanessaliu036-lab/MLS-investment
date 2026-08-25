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
MIN_TRAILING_N = 60            # 低於此不給統計,標 insufficient

# 四層分級門檻(見 CLAUDE.md Research Lead 章程第 9 條)
PRIMARY_POSITIVE_RATE = 55.0
HP_PF = 1.8
HP_WIN_LOSS = 2.0
HP_EXPECTED_UPSIDE = 5.0
HP_HIT_RATE = 65.0             # P(+3%) 高於獨立窗 in_top10 水準

VERSION = "opportunity_score_v1_2026-08-24"
EVIDENCE_LEVEL = "REPLICATED — PENDING LIVE"

# ── 個股歷史不足時的回退基準 ──────────────────────────────────────
# 來源:2020-07~2023-12 獨立窗(FROZEN_OPPORTUNITY_TARGET_V1.md 的保守估計)。
# ⚠ 為什麼需要:引擎 daily_bar 目前只有約 20 個交易日歷史,個股自身統計
#    (需 >= MIN_TRAILING_N=60)全部不足 → 全部落 WATCH,分層失去意義。
#    回退時報的是「該訊號狀態的歷史條件統計」,不是這一檔自己的統計,
#    因此 stats_basis 必須標成 "conditional_fallback",UI 不得混為一談。
#    引擎歷史累積足夠後會自動改用個股自身統計(per_stock)。
CONDITIONAL_FALLBACK = {
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
                               window: int = TRAILING_WINDOW) -> dict:
    """由個股自己近 `window` 個交易日的**已實現**結果,算六項原始指標。

    這是事實描述(這檔近期的機會結構長什麼樣),不是預測模型 ——
    刻意不擬合任何東西,避免又變成一輪 discovery。

    bars 需含 date/open/high/low/close,依日期排序。只用已到期的樣本。
    """
    n = len(bars)
    hits, mfes, maes, terms = [], [], [], []
    # 只取「進場日 + horizon 已完成」的樣本
    last_complete = n - horizon - 1
    start = max(0, last_complete - window + 1)
    for i in range(start, last_complete + 1):
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

    if len(hits) < MIN_TRAILING_N:
        return {"n": len(hits), "insufficient": True}

    wins = [t for t in terms if t > 0]
    losses = [t for t in terms if t <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None
    hit_mfes = [m for m, h in zip(mfes, hits) if h]
    return {
        "n": len(hits),
        "insufficient": False,
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


def assign_tier(stats: dict, in_top_sector: bool) -> tuple[str, list[str]]:
    """四層分級。回傳 (tier, 理由清單)。

    ⚠ 55% 是主榜**資格線**不是刪除線 —— 勝率不足但 payoff 結構好的,
       一律留在 HIGH_POTENTIAL,不得丟掉。
    """
    if stats.get("insufficient"):
        return "WATCH", ["樣本不足,僅列出不分級"]

    # 統計採條件回退值時,所有股票的六項指標完全相同(那是族群狀態的
    # 條件統計,不是個股差異)。此時唯一真實的個股資訊只有訊號狀態本身,
    # 拿共用常數去分四層會製造假的區別度 —— 只依訊號分兩層,誠實標示。
    if stats.get("stats_basis") == "conditional_fallback":
        if in_top_sector:
            return "HIGH_POTENTIAL", ["族群 sec_rs_10d Top10%(凍結訊號觸發)"]
        return "WATCH", ["族群未進 Top10%"]

    reasons = []
    pos = stats.get("net_positive_rate") or 0
    pf = stats.get("profit_factor") or 0
    up = stats.get("expected_upside") or 0
    hit = stats.get("p_hit_3pct") or 0
    aw, al = stats.get("avg_win"), stats.get("avg_loss")
    wl = (aw / abs(al)) if (aw and al and al != 0) else 0

    # 高 payoff 特徵(任一即可留 HIGH_POTENTIAL)
    hp_hits = []
    if hit >= HP_HIT_RATE:
        hp_hits.append(f"P(+3%)={hit:.1f}%")
    if pf >= HP_PF:
        hp_hits.append(f"PF={pf:.2f}")
    if wl >= HP_WIN_LOSS:
        hp_hits.append(f"賺賠比={wl:.2f}")
    if up >= HP_EXPECTED_UPSIDE:
        hp_hits.append(f"ExpUpside={up:.1f}%")

    if pos >= PRIMARY_POSITIVE_RATE and in_top_sector:
        reasons.append(f"勝率 {pos:.1f}% 達主榜線 + 族群 Top10%")
        return "PRIMARY", reasons
    if pos >= PRIMARY_POSITIVE_RATE:
        reasons.append(f"勝率 {pos:.1f}% 達主榜線,但族群未進 Top10%")
        return "HIGH_POTENTIAL", reasons
    if hp_hits:
        reasons.append(f"勝率 {pos:.1f}% 未達主榜線,但 payoff 結構強:" + "、".join(hp_hits))
        if in_top_sector:
            reasons.append("且族群 Top10%")
        return "HIGH_POTENTIAL", reasons
    # 明顯不利才進 AVOID:期望值為負且下檔深
    if (stats.get("net_expectancy") or 0) < 0 and (stats.get("expected_downside") or 0) < -8:
        reasons.append("期望值為負且下檔深")
        return "AVOID", reasons
    reasons.append(f"勝率 {pos:.1f}%、無突出 payoff 特徵")
    return "WATCH", reasons


def score_one(code: str, bars: list[dict], sector_bars: dict[str, list],
              sector_rank_pct: Optional[float], stage: Optional[str]) -> dict:
    """單檔的完整 production 計分。"""
    in_top = (sector_rank_pct is not None and sector_rank_pct > SECTOR_TOP_PCT)
    excluded = (stage == "EXTENDED")
    s10 = realized_opportunity_stats(bars, 10)
    s15 = realized_opportunity_stats(bars, 15)
    # 個股歷史不足 → 回退到該訊號狀態的歷史條件統計,並明確標示基礎
    if s10.get("insufficient"):
        s10 = {**CONDITIONAL_FALLBACK[bool(in_top)], "n": s10.get("n", 0),
               "insufficient": False, "stats_basis": "conditional_fallback"}
    else:
        s10["stats_basis"] = "per_stock"
    if s15.get("insufficient"):
        s15 = {**s15, "stats_basis": "conditional_fallback_unavailable"}
    else:
        s15["stats_basis"] = "per_stock"
    tier, reasons = assign_tier(s10, in_top and not excluded)
    if s10.get("stats_basis") == "conditional_fallback":
        reasons.append("⚠ 個股歷史不足,統計採歷史條件回退值(非本檔自身)")
    if excluded:
        tier, reasons = "AVOID", ["Pre-Activation EXTENDED(已漲太多,不追)"]
    return {
        "code": code,
        "signal_in_top_sector": bool(in_top),
        "sector_rank_pct": (round(sector_rank_pct, 4)
                            if sector_rank_pct is not None else None),
        "pa_stage": stage,
        "tier": tier,
        "tier_reasons": reasons,
        "evidence_level": EVIDENCE_LEVEL,
        "t10": s10,
        "t15": s15,
        "score_version": VERSION,
    }
