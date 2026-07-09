"""
tss_scheduler.py — TSS v1.0 獨立排程

設計原則:
    - 不動主系統 after_hours.py / engine.py / scoring.py
    - 獨立 cron 模式,用 Python schedule + sleep 走交易日內時序
    - 排程動作:
        13:40  盤後篩選 (run_after_market_filter) — 跑 C1~C4 拿合格標的池
        09:00~13:30  盤中 tick loop 對合格池監控
        週四 17:00  集保 CSV 抓取 (規格書第九章 SOP)

執行:
    python3 tss_scheduler.py --dry-run
    python3 tss_scheduler.py --watchlist 2330 2454 2603

邊界:
    - 不寫下單 (規格書只到進場訊號層,實際下單未實作)
    - 不存持倉 / 不寫 DB
    - 報表統一寫進 reports/TSS_YYYYMMDD.md (跟 main.py 同路徑)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from tss_mvp import (
    PARAMS,
    HERE,
    MONEY_HEALTH_DIR,
    MLS_DB_PATH,
    fetch_1min_kbars,
    fetch_index_daily,
    fetch_institutional,
    fetch_big_holder,
    classify_buy_sell_vol,
    filter_after_market,
    generate_mock_1m_kbars,
    mock_index_daily,
    mock_institutional,
    run_intraday_loop,
    shioaji_login,
    get_universe,
    get_chips_from_money_health,
    write_watchlist_to_mls_db,
    read_watchlist_from_mls_db,
)


def is_trading_day(now: datetime) -> bool:
    """簡易判斷: 週一到週五 = 交易日 (不扣國定假日,MVP 不嚴格)。"""
    return now.weekday() < 5


def run_after_market_filter(codes: list, days: int, dry_run: bool, output_dir: Path) -> dict:
    """盤後篩選主程式 — 回傳 watchlist dict[code] -> filter result。"""
    print(f"\n[{datetime.now():%H:%M:%S}] 盤後篩選啟動 ({len(codes)} 檔)")

    if dry_run:
        index_df = mock_index_daily(days=120)
        inst_df_map = {c: mock_institutional(c, days=days) for c in codes}
        bh_df_map = {}  # dry-run 跳過集保
        stock_1m_map = {c: generate_mock_1m_kbars(days=days) for c in codes}
    else:
        api = shioaji_login()
        contract_map = {c: api.Contracts.Stocks.TSE[c] for c in codes}
        index_df = fetch_index_daily(api, days=120)
        inst_df_map = {c: fetch_institutional(c, days=max(days, 30)) for c in codes}
        bh_df_map = {c: fetch_big_holder(c) for c in codes}
        end = datetime.now()
        start = end - timedelta(days=days)
        stock_1m_map = {
            c: fetch_1min_kbars(api, contract_map[c], start, end) for c in codes
        }
        try:
            api.logout()
        except Exception:
            pass

    watchlist: Dict[str, dict] = {}
    output_dir.mkdir(parents=True, exist_ok=True)

    for code in codes:
        df_1m = classify_buy_sell_vol(stock_1m_map[code])
        result = filter_after_market(
            df_1m,
            index_df_daily=index_df,
            big_holder_df=bh_df_map.get(code),
            institutional_df=inst_df_map.get(code),
        )
        result["code"] = code
        watchlist[code] = result

        flag = "✅" if result["final_signal"] else "❌"
        print(f"   {flag} {code}: bs={result['bs_ratio_daily']:.2f} "
              f"close={result.get('close')} signal={result['final_signal']}")

    # 寫當日 watchlist 報表
    report_path = output_dir / f"TSS_WATCHLIST_{datetime.now().strftime('%Y%m%d')}.md"
    lines = [
        f"# TSS v1.0 明日 Watchlist — {datetime.now():%Y-%m-%d}",
        f"",
        f"執行時間: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"",
        f"## 篩選結果",
        f"",
        f"| 代號 | 收盤 | MA20 | 乖離% | BS Ratio | Final |",
        f"|---|---|---|---|---|---|",
    ]
    for code, r in watchlist.items():
        lines.append(
            f"| {code} | {r.get('close')} | {r.get('ma20')} | "
            f"{r.get('bias_pct')} | {r.get('bs_ratio_daily')} | "
            f"{'✅ BUY' if r['final_signal'] else '❌ wait'} |"
        )
    lines.append("")
    qualified = [c for c, r in watchlist.items() if r["final_signal"]]
    lines.append(f"## 合格標的池 ({len(qualified)} 檔)")
    lines.append("")
    if qualified:
        lines.append(", ".join(qualified))
    else:
        lines.append("(無 — 今日無標的通過四因子)")
    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n📄 Watchlist 報表: {report_path}")
    print(f"🎯 明日合格: {len(qualified)} 檔")

    # ── 跟 mls.db 對接:把合格標的寫進共用 watchlist 表 ──
    if not dry_run and qualified:
        written = write_watchlist_to_mls_db(
            watchlist, reason_prefix="TSS_v1"
        )
        print(f"💾 寫入 mls.db watchlist: {written} 筆")
    elif dry_run:
        print(f"💾 [DRY-RUN] 略過寫入 mls.db")

    return watchlist


def run_intraday_monitor(codes: list, duration_min: int, dry_run: bool):
    """盤中 tick 監控。"""
    if dry_run:
        print(f"[DRY-RUN] 盤中監控 {codes} {duration_min} 分鐘 (略)")
        return

    api = shioaji_login()

    def on_decision(code: str, snap):
        # 簡化: 收 BS Ratio > 1.2 就印 trigger
        if snap.bs_ratio_5min > 0.55:
            print(f"   ⚡ {code} 5min BS Ratio {snap.bs_ratio_5min:.1%} (注意)")

    try:
        run_intraday_loop(api, codes, duration_min=duration_min, on_decision=on_decision)
    finally:
        try:
            api.logout()
        except Exception:
            pass


def main():
    # 預設 watchlist 從「盤後資金健康度」共用 UNIVERSE,沒有就走 3 檔預設
    default_watchlist = get_universe() if len(sys.argv) == 1 or "--watchlist" not in sys.argv else None
    if default_watchlist is None:
        default_watchlist = ["2330", "2454", "2603"]

    p = argparse.ArgumentParser(description="TSS v1.0 獨立排程")
    p.add_argument("--dry-run", action="store_true", help="用 mock 跑")
    p.add_argument(
        "--watchlist", nargs="+", default=default_watchlist,
        help=f"監控清單 (預設從「盤後資金健康度/config.py」讀 UNIVERSE,共 {len(default_watchlist)} 檔)",
    )
    p.add_argument("--days", type=int, default=30, help="回測天數")
    p.add_argument("--mode", choices=["after_market", "intraday", "all"], default="after_market",
                   help="after_market=盤後篩選;intraday=盤中監控;all=兩個都跑")
    p.add_argument("--intraday-min", type=int, default=240, help="盤中監控時長(分)")
    args = p.parse_args()

    print(f"🚀 TSS v1.0 Scheduler 啟動")
    print(f"   Watchlist: {args.watchlist} | 模式: {args.mode} | DRY-RUN: {args.dry_run}")

    output_dir = HERE / "reports"

    if args.mode in ("after_market", "all"):
        if not is_trading_day(datetime.now()) and not args.dry_run:
            print("⚠️  非交易日,跳過盤後篩選")
        else:
            watchlist = run_after_market_filter(args.watchlist, args.days, args.dry_run, output_dir)
            qualified = [c for c, r in watchlist.items() if r["final_signal"]]
            if args.mode == "all" and qualified and not args.dry_run:
                print(f"\n⏰ 切到盤中監控 ({qualified})")
                run_intraday_monitor(qualified, args.intraday_min, args.dry_run)

    if args.mode == "intraday" and not args.dry_run:
        run_intraday_monitor(args.watchlist, args.intraday_min, args.dry_run)

    print("\n✅ Scheduler 結束")


if __name__ == "__main__":
    main()