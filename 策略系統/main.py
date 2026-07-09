"""
main.py — TSS v1.0 啟動入口

用途:
    跑 TSS v1.0 整個 MVP 流程:
      1. 登入永豐金 Shioaji
      2. 抓個股 1 分 K (自動分段)
      3. 計算 Buy/Sell Vol
      4. 跑四因子盤後篩選
      5. 印出報表 + 寫到 reports/TSS_YYYYMMDD.md

執行:
    python3 main.py --dry-run
    python3 main.py --code 2330 --days 30
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# 把這個檔所在目錄加入 path,讓 tss_mvp 可 import
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from tss_mvp import (
    PARAMS,
    TSSParams,
    ShioajiActiveVolumeTracker,
    fetch_1min_kbars,
    fetch_index_daily,
    fetch_institutional,
    classify_buy_sell_vol,
    filter_after_market,
    generate_mock_1m_kbars,
    mock_index_daily,
    mock_institutional,
    shioaji_login,
)


def run_pipeline(code: str, days: int, dry_run: bool, output_dir: Path):
    print(f"🚀 TSS v1.0 啟動")
    print(f"   標的: {code} | 天數: {days} | 模式: {'DRY-RUN' if dry_run else 'LIVE'}")

    api = None
    if dry_run:
        stock_1m = generate_mock_1m_kbars(days=days)
        index_df = mock_index_daily(days=120)
        inst_df = mock_institutional(code, days=days)
        print(f"📦 Mock 資料: {len(stock_1m)} 根 1 分 K | {len(index_df)} 日大盤 | {len(inst_df)} 日法人")
    else:
        api = shioaji_login()
        contract = api.Contracts.Stocks.TSE[code]
        from datetime import timedelta
        end = datetime.now()
        start = end - timedelta(days=days)
        stock_1m = fetch_1min_kbars(api, contract, start, end)
        index_df = fetch_index_daily(api, days=120)
        inst_df = fetch_institutional(code, days=max(days, 30))
        print(f"📥 個股 1 分 K: {len(stock_1m)} 根 | 大盤日 K: {len(index_df)} 日 | 法人日資料: {len(inst_df)} 日")

    stock_1m = classify_buy_sell_vol(stock_1m)
    total_buy = int(stock_1m["Buy_Vol"].sum())
    total_sell = int(stock_1m["Sell_Vol"].sum())
    bs_ratio = round(total_buy / total_sell, 3) if total_sell else 0
    print(f"💰 Buy Vol: {total_buy:,} | Sell Vol: {total_sell:,} | BS Ratio: {bs_ratio}")

    result = filter_after_market(
        stock_1m,
        index_df_daily=index_df,
        institutional_df=inst_df,
    )

    if api is not None:
        try:
            api.logout()
        except Exception:
            pass

    # 印報表
    print()
    print("=" * 60)
    print(f"📊 TSS v1.0 篩選結果 — {code}")
    print("=" * 60)
    print(f"   進場日:   {result.get('trade_date')}")
    print(f"   收盤價:   {result.get('close')}")
    print(f"   MA20:     {result.get('ma20')}")
    print(f"   乖離率:   {result.get('bias_pct')}%")
    print(f"   BS Ratio (日): {result.get('bs_ratio_daily')}")
    print(f"   條件閾值:    {PARAMS.bs_ratio_threshold}")
    print()
    print("條件明細:")
    for cond_name, cond_val in result.get("conditions", {}).items():
        print(f"   • {cond_name}: {cond_val}")
    print()
    print(f"🎯 Final Signal: {result.get('final_signal')}")
    if result.get("force_stop"):
        print(f"🛑 Force Stop: {result.get('force_stop_reason')}")

    # 寫報表到 reports/
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"TSS_{datetime.now().strftime('%Y%m%d')}.md"
    write_report(report_path, code, result)
    print(f"\n📄 報表已寫: {report_path}")

    return result


def write_report(path: Path, code: str, result: dict) -> None:
    lines = [
        f"# TSS v1.0 篩選報告 — {code}",
        f"",
        f"執行時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
        f"## 結果",
        f"",
        f"- 進場日: {result.get('trade_date')}",
        f"- 收盤價: {result.get('close')}",
        f"- MA20: {result.get('ma20')}",
        f"- 乖離率: {result.get('bias_pct')}%",
        f"- Buy Vol: {result.get('buy_vol'):,}",
        f"- Sell Vol: {result.get('sell_vol'):,}",
        f"- BS Ratio (日): {result.get('bs_ratio_daily')}",
        f"- 條件閾值: {PARAMS.bs_ratio_threshold}",
        f"",
        f"## 條件明細",
        f"",
    ]
    for name, val in result.get("conditions", {}).items():
        lines.append(f"### {name}")
        lines.append(f"```")
        lines.append(str(val))
        lines.append("```")
        lines.append("")
    lines.append(f"## 最終訊號")
    lines.append("")
    lines.append(f"- Final Signal: **{result.get('final_signal')}**")
    if result.get("force_stop"):
        lines.append(f"- Force Stop: {result.get('force_stop_reason')}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description="TSS v1.0 全系統啟動入口")
    p.add_argument("--code", default="2330", help="個股代號")
    p.add_argument("--days", type=int, default=30, help="回測天數")
    p.add_argument("--dry-run", action="store_true", help="用 mock 跑,不登入券商")
    p.add_argument(
        "--output-dir",
        default=str(HERE / "reports"),
        help="報表輸出目錄 (default: ./reports)",
    )
    args = p.parse_args()

    output_dir = Path(args.output_dir)
    run_pipeline(args.code, args.days, args.dry_run, output_dir)


if __name__ == "__main__":
    main()