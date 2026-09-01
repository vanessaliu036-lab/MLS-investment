"""Build front-end cards from database rows only."""
from __future__ import annotations

from .analyzer import AnalysisInput, analyze
from .history import rescue_validation, scenario_stats
from .repository import (
    aflow_positive_two_samples,
    chip_4d_summary,
    consecutive_flow_ticks,
    current_top_rows,
    latest_trigger_context,
    market_regime,
    threshold_for_symbol,
)


def build_top10(conn, trade_date: str, direction: str, top_n: int = 10) -> dict:
    rows = current_top_rows(conn, trade_date, direction, limit=max(top_n * 2, 20))
    regime = market_regime(conn, trade_date)
    rescue_ok = rescue_validation(conn)["approved"]
    cards = []
    for row in rows:
        threshold, configured_ticks = threshold_for_symbol(conn, row["symbol"])
        consecutive = consecutive_flow_ticks(conn, row["symbol"], trade_date, threshold)
        chip = chip_4d_summary(conn, row["symbol"], trade_date)
        trig = latest_trigger_context(conn, row["symbol"], trade_date)
        inp = AnalysisInput(
            symbol=row["symbol"],
            name=row["stock_name"],
            trade_date=trade_date,
            price_data_date=row["price_data_date"],
            chip_data_date=chip["chip_data_date"],
            flow_data_time=row["flow_data_time"] or row["ts"],
            snapshot_time=row["as_of"],
            net_flow_amount=row["net_flow_amount"],
            flow_threshold=threshold,
            flow_consecutive_ticks=consecutive,
            price_change_pct=row["price_change_pct"],
            high=row["high"], low=row["low"], close=row["close"], prev_close=row["prev_close"],
            vwap=row["vwap"], volume=row["volume"], ma5_volume=row["ma5_volume"],
            net_active=row["net_active"],
            aflow_positive_2_samples=aflow_positive_two_samples(conn, row["symbol"], trade_date),
            foreign_net_4d=chip["foreign_net_4d"], volume_4d=chip["volume_4d"],
            big_holder_trend=chip["big_holder_trend"],
            trigger_failed=trig["trigger_failed"], trigger_passed=trig["trigger_passed"],
            market_regime=regime["regime"], rescue_rule_approved=rescue_ok,
        )
        result = analyze(inp).to_dict()
        result.update({
            "net_flow_amount": row["net_flow_amount"],
            "price_change_pct": row["price_change_pct"],
            "turnover_ratio": row["turnover_ratio"],
            "flow_threshold": threshold,
            "required_ticks": configured_ticks,
            "flow_consecutive_ticks": consecutive,
            "chip_data_date": chip["chip_data_date"],
            "price_data_date": row["price_data_date"],
            "flow_data_time": row["flow_data_time"] or row["ts"],
            "snapshot_time": row["as_of"],
            "trigger_context": trig,
        })
        result["history"] = scenario_stats(conn, result["scenario"], regime["regime"])
        cards.append(result)
    return {
        "trade_date": trade_date,
        "direction": direction,
        "market_regime": regime["regime"],
        "rescue_validation": rescue_validation(conn),
        "results": cards[:top_n],
    }
