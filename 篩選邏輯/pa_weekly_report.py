#!/usr/bin/env python3
"""Pre-Activation v0.1 — 每週 Live Observation 報告(排程用,2026-08-24 起)。

歷史模型研究已結案(F4 分類 / TRIGGER 手排規則 / High-Payoff 連續特徵回歸,
三種方法論都沒找到穩健 edge —— 見 winning_model_backtest/FROZEN_F4.md 與
專案記憶 pa-trigger-no-edge-vs-baseline、pa-high-payoff-v1-no-edge)。

現在只做三件事,不做第四件:
  1. 每天存 51 檔的 stage(run_pa_snapshot.py,已排程,這支不重複做)
  2. 自動回填 T+1/3/5/7 gross/net/MFE/MAE(pa_snapshot.backfill(),同上)
  3. 每週出這一張報告 —— 只讀 pa_snapshot,不重算、不因為幾筆結果就調規則

重新決策門檻(寫死,不因為看到漂亮數字就提前下結論):
任一 stage 的 T+5 樣本數 >= pa_report.MIN_N(20)才算「這個 stage 有意見」;
到門檻那天最優先看的是 EARLY 的 Net Expectancy / PF / MFE-MAE,以及
EARLY→ARMED/TRIGGER 轉換後是否比「停在 EARLY」更好——這是回測發現
「高 payoff 64% 落在 EARLY」在 live 資料上的第一次真實檢驗。

若門檻到了、EARLY 依然沒有優勢:不是模型還沒調好,是這 51 檔的既有日線
欄位(MA/量比/突破/外資/相對強弱)本身不夠預測下一段 payoff——下一步
換資訊(盤中資金結構/法人行為變化率/壓縮-釋放結構),不是換模型或擴母體
到 1500 檔繼續排列同一組特徵。
"""
from __future__ import annotations
import datetime as dt
import os
import sys

import pa_report

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def render(db_path: str = "mls.db") -> str:
    today = dt.date.today().isoformat()
    lines = [
        f"# Pre-Activation v0.1 — Live Observation 週報 ({today})",
        "",
        "歷史模型研究已結案:F4 / TRIGGER 手排規則 / High-Payoff 連續特徵回歸,",
        "三種方法論皆未找到穩健 edge。現在只累積 live 樣本,不調規則、",
        "不因幾筆結果換模型。", "",
        "## 各 stage 現況(T+5,不足 20 筆只列出不下結論)", "",
        pa_report.summary_text(db_path), "",
    ]

    d = pa_report.by_stage(db_path)
    lines += ["## 完整明細(T+3 / T+5 / T+7)", ""]
    for st in pa_report.STAGES:
        rec = d.get(st) or {}
        lines.append(f"### {st}  (待回填 {rec.get('pending', 0)} 筆)")
        for h in (3, 5, 7):
            s = rec.get(f"T+{h}")
            if not s:
                lines.append(f"  T+{h}: 尚無到期樣本")
                continue
            tag = "" if s["enough"] else " ⚠樣本不足(<20),僅列出不下結論"
            lines.append(
                f"  T+{h}: n={s['n']} 淨期望={s['net_expectancy']:+.3f}% "
                f"PF={s['profit_factor'] if s['profit_factor'] is not None else '—'} "
                f"正報酬率={s['positive_rate']}% 賺{s['avg_win']}/賠{s['avg_loss']} "
                f"最大單筆虧損={s['max_loss']}%{tag}")
        lines.append("")

    lines += ["## 轉換分析(早進場 vs 等確認,T+5)", ""]
    et = pa_report.early_transitions(db_path)
    up, stay = et["early_upgraded"], et["early_stayed"]
    if up or stay:
        lines.append(f"EARLY→ARMED/TRIGGER 後: n={up.get('n', 0)} "
                     f"淨期望={up.get('net_expectancy', '—')}")
        lines.append(f"EARLY 停留未升級:      n={stay.get('n', 0)} "
                     f"淨期望={stay.get('net_expectancy', '—')}")
    else:
        lines.append("尚無足夠樣本比較。")
    lines.append("")
    at = pa_report.armed_to_trigger(db_path)
    up2, stay2 = at["armed_to_trigger"], at["armed_stayed"]
    if up2 or stay2:
        lines.append(f"ARMED→TRIGGER 後:      n={up2.get('n', 0)} "
                     f"淨期望={up2.get('net_expectancy', '—')}")
        lines.append(f"ARMED 停留未升級:      n={stay2.get('n', 0)} "
                     f"淨期望={stay2.get('net_expectancy', '—')}")
    else:
        lines.append("尚無足夠樣本比較。")

    lines += ["", "## 重新決策門檻", "",
             f"EARLY 的 T+5 樣本數達 {pa_report.MIN_N} 筆前,以上數字僅供觀察,",
             "不作為任何規則調整或前台改動的依據。", ""]
    return "\n".join(lines)


def main() -> int:
    try:
        text = render()
        os.makedirs(REPORT_DIR, exist_ok=True)
        path = os.path.join(REPORT_DIR, f"pa_weekly_{dt.date.today().isoformat()}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print(text)
        print(f"\n[pa_weekly_report] 已存: {path}", flush=True)
        return 0
    except Exception as e:
        print(f"[pa_weekly_report] 失敗: {type(e).__name__}: {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
