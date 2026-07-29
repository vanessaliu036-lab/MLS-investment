# -*- coding: utf-8 -*-
"""
模型優化建議（Phase 5 · 離線建議制）
────────────────────────────────────────────────────────────
把 Phase 1~4 每日留痕的資料（review_log / watch_outcome / watch_reject /
rule_signals）跑一次統計，產出「人看得懂的優化建議」。

原則（使用者定案）：
  · 只讀不寫 —— 絕不改 config.py 的門檻/權重，也不動任何名單。
  · 只出建議 —— 印出報表與建議，是否採用由你人工判斷後手動改常數。
  · 資料不足就擋 —— 交易日數 < MIN_DAYS 時只印進度，不下結論（避免過擬合）。

用法：
    python 模型優化建議.py                # 用預設 DB 路徑、預設門檻
    python 模型優化建議.py --min-days 20  # 自訂最少交易日
    MLS_DB=/path/mls.db EOD_DB=/path/eod.db python 模型優化建議.py

建議累積至少 20~30 個交易日（約 1~1.5 個月）再認真看數字。
"""

import os
import sys
import json
import sqlite3
import argparse
from pathlib import Path
from statistics import mean

BASE = Path(__file__).resolve().parent
MLS_DB = os.environ.get("MLS_DB", str(BASE / "個股卡片相關檔案_20260722" / "mls.db"))
EOD_DB = os.environ.get("EOD_DB", str(BASE / "intraday_eod.db"))

MIN_DAYS = 15          # 低於此天數不下結論
GAP_ALERT = 25         # 真實命中 vs 含續強 差距(百分點)超過即示警
STRICT_SCORE = 55      # 落選者平均總分高於此，卻大量卡同一因子 → 疑門檻過嚴

# radar 七因子可讀標籤（factors_json 的 key → 中文）
FACTOR_LABEL = {"money_health": "資金健康", "absorption": "吸貨",
                "net_active": "主動買賣差", "vs_ma20": "站上MA20",
                "inst_streak": "法人連買", "margin": "融資"}


def _ro(path):
    """唯讀開啟；檔案不存在回 None。"""
    if not os.path.exists(path):
        return None
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _has_table(c, name):
    return bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


def _pct(n, d):
    return round(n / d * 100, 1) if d else None


def section(title):
    print("\n" + "─" * 60)
    print(f"◆ {title}")
    print("─" * 60)


def analyze():
    print("=" * 60)
    print("  MLS 模型優化建議（離線 · 只讀 · 只出建議）")
    print("=" * 60)
    print(f"mls.db : {MLS_DB}")
    print(f"eod.db : {EOD_DB}")

    mls = _ro(MLS_DB)
    if mls is None:
        print(f"\n找不到 mls.db：{MLS_DB}")
        return

    # ── 資料涵蓋 ───────────────────────────────────────────
    section("資料涵蓋")
    days, rng = 0, ("—", "—")
    if _has_table(mls, "watch_outcome"):
        r = mls.execute("""SELECT COUNT(DISTINCT trade_date) d,
                                  MIN(trade_date) a, MAX(trade_date) b
                           FROM watch_outcome""").fetchone()
        days, rng = r["d"] or 0, (r["a"] or "—", r["b"] or "—")
    print(f"watch_outcome 交易日數：{days}（{rng[0]} ~ {rng[1]}）")
    suggestions = []
    enough = days >= MIN_DAYS
    if not enough:
        print(f"⚠ 交易日數 < {MIN_DAYS}，以下只列現況、不下優化結論"
              f"（避免樣本太少過擬合）。還需約 {MIN_DAYS - days} 個交易日。")

    # ── 來源分流命中率（watch_outcome）────────────────────
    section("來源分流命中率（T+1）")
    if _has_table(mls, "watch_outcome"):
        rows = [dict(r) for r in mls.execute(
            "SELECT * FROM watch_outcome")]
        for src, label in (("radar", "雷達(可操作/觀察)"), ("resilient", "抗跌")):
            # source 存在 watchlist，watch_outcome 沒直接存；用 verdict 反推族群
            sub = [r for r in rows if _src_of(r) == src]
            if not sub:
                print(f"{label}: 尚無資料")
                continue
            n = len(sub)
            A = sum(1 for r in sub if _tier(r["verdict"]) == "A")
            B = sum(1 for r in sub if _tier(r["verdict"]) == "B")
            print(f"{label}: {n} 筆 · 真實命中A {_pct(A,n)}% · 含續強A+B {_pct(A+B,n)}%")
            if enough and src == "radar":
                gap = _pct(A + B, n) - _pct(A, n) if n else 0
                if gap >= GAP_ALERT:
                    suggestions.append(
                        f"雷達：含續強({_pct(A+B,n)}%)遠高於真實命中({_pct(A,n)}%)，"
                        f"差 {round(gap,1)}pp → 抓得到方向、抓不到爆發力；"
                        f"建議調高『量比 / 主動買賣差 / 籌碼』權重，篩掉只跟漲的。")
    else:
        print("watch_outcome 尚無資料（Phase 3 上線後每日 18:00 累積）")

    # ── 報酬分布（review_log）─────────────────────────────
    section("逐日命中率 × 報酬分布（review_log）")
    if _has_table(mls, "review_log"):
        rl = [dict(r) for r in mls.execute(
            "SELECT * FROM review_log WHERE hit_rate IS NOT NULL "
            "ORDER BY trade_date DESC LIMIT 30")]
        if rl:
            print(f"{'交易日':<12}{'命中率':>7}{'平均':>8}{'中位':>8}{'最佳':>8}{'最差':>8}  版本")
            for r in rl[:15]:
                print(f"{r['trade_date']:<12}{_f(r['hit_rate'],'%'):>7}"
                      f"{_f(r.get('avg_return'),'%'):>8}{_f(r.get('median_return'),'%'):>8}"
                      f"{_f(r.get('max_return'),'%'):>8}{_f(r.get('min_return'),'%'):>8}"
                      f"  {r.get('model_version') or '—'}")
            avgs = [r["avg_return"] for r in rl if r.get("avg_return") is not None]
            hrs = [r["hit_rate"] for r in rl if r.get("hit_rate") is not None]
            if enough and avgs and hrs:
                mh, ma = mean(hrs), mean(avgs)
                if mh >= 55 and ma < 0.5:
                    suggestions.append(
                        f"高勝率({round(mh,1)}%)但平均報酬低({round(ma,2)}%)：勝率有了沒肉 → "
                        f"可拉高 RADAR_T1_SUCCESS 門檻只留有肉的，或加動能因子。")
                if mh < 45 and ma >= 1.0:
                    suggestions.append(
                        f"低勝率({round(mh,1)}%)高報酬({round(ma,2)}%)：少數大賺型 → "
                        f"維持寬門檻、靠分散，不要為提高勝率砍掉爆發股。")
        else:
            print("review_log 尚無含命中率的紀錄。")

    # ── 落選因子分析（watch_reject）── §9-F 的主要用途 ─────
    section("落選因子分析（門檻鬆緊診斷）")
    if _has_table(mls, "watch_reject"):
        wr = [dict(r) for r in mls.execute("SELECT * FROM watch_reject")]
        if wr:
            total = len(wr)
            by_fail = {}
            for r in wr:
                by_fail.setdefault(r["fail_factor"] or "—", []).append(r)
            print(f"落選共 {total} 檔，依卡住因子：")
            print(f"{'卡在':<22}{'檔數':>6}{'占比':>7}{'平均總分':>9}")
            for fail, sub in sorted(by_fail.items(), key=lambda x: -len(x[1])):
                scores = [r["score_total"] for r in sub if r.get("score_total") is not None]
                avg_s = round(mean(scores), 1) if scores else None
                share = _pct(len(sub), total)
                print(f"{fail:<22}{len(sub):>6}{share:>6}%{_f(avg_s):>9}")
                if enough and avg_s is not None and share >= 30 and avg_s >= STRICT_SCORE:
                    suggestions.append(
                        f"落選因子『{fail}』佔 {share}% 且平均總分 {avg_s} 偏高 → "
                        f"這些股其實整體不差卻被此因子刷掉，門檻可能過嚴，建議放寬。")
        else:
            print("watch_reject 尚無資料（Phase 1 上線後每日 18:00 累積）")

    # ── 分類規則同日命中率（rule_signals）─────────────────
    section("分類規則命中率（rule_signals · 同日）")
    eod = _ro(EOD_DB)
    if eod and _has_table(eod, "rule_signals"):
        for r in eod.execute(
            """SELECT category, COUNT(*) n, SUM(result='命中') hit
               FROM rule_signals WHERE result IN ('命中','未命中')
               GROUP BY category"""):
            print(f"{r['category']:<8} {r['n']:>4} 筆 · 命中 {r['hit']} "
                  f"（{_pct(r['hit'], r['n'])}%）")
    else:
        print("rule_signals 尚無資料。")
    if eod:
        eod.close()
    mls.close()

    # ── 建議彙總 ───────────────────────────────────────────
    section("優化建議（僅供人工參考，不自動套用）")
    if not enough:
        print("資料量不足，暫不提出調整建議。請持續累積後再跑。")
    elif suggestions:
        for i, s in enumerate(suggestions, 1):
            print(f"{i}. {s}")
        print("\n★ 以上為建議。要採用請『人工』修改 config.py 的門檻/權重常數，")
        print("  改完重新部署，並在 review_log 用新的 model_version 分版比較效果。")
    else:
        print("目前數據未觸發任何調整建議，維持現行門檻。")
    print()


def _src_of(outcome_row):
    """watch_outcome 沒存 source，用 verdict 反推來源族群。"""
    v = str(outcome_row.get("verdict") or "")
    if v.startswith("A_") or v.startswith("B_") or v.startswith("C_") \
            or v in ("突破延續", "突破站穩", "突破失敗", "未突破"):
        return "radar"
    if v.startswith("抗跌"):
        return "resilient"
    return "other"


def _tier(verdict):
    v = str(verdict or "")
    if v.startswith("A_") or v in ("抗跌成立", "相容命中", "突破延續"):
        return "A"
    if v.startswith("B_") or v == "突破站穩":
        return "B"
    return "C"


def _f(x, suffix=""):
    return "—" if x is None else f"{x}{suffix}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-days", type=int, default=MIN_DAYS)
    args = ap.parse_args()
    MIN_DAYS = args.min_days
    analyze()
