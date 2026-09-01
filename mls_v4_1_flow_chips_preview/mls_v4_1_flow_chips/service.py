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
    expected_chip_date,
    current_latest_rows,
    institutional_outflow_summary,
    aflow_persistence_metrics,
)


def build_top10(conn, trade_date: str, direction: str, top_n: int = 10) -> dict:
    rows = current_top_rows(conn, trade_date, direction, limit=max(top_n * 2, 20))
    regime = market_regime(conn, trade_date)
    rescue_ok = rescue_validation(conn)["approved"]
    expected_chip = expected_chip_date(conn, trade_date)
    cards = []
    for row in rows:
        threshold, configured_ticks = threshold_for_symbol(conn, row["symbol"])
        consecutive = consecutive_flow_ticks(conn, row["symbol"], trade_date, threshold)
        chip = chip_4d_summary(conn, row["symbol"], trade_date)
        chip_is_fresh = bool(expected_chip and chip["chip_data_date"] == expected_chip)
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
            foreign_net_4d=chip["foreign_net_4d"] if chip_is_fresh else None,
            volume_4d=chip["volume_4d"] if chip_is_fresh else None,
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
            "chip_expected_date": expected_chip,
            "chip_stale_data": not chip_is_fresh,
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


def _control_note(conn, symbol: str, trade_date: str) -> str | None:
    row = conn.execute(
        """SELECT source_note FROM trigger_context
           WHERE symbol=? AND trade_date <= ?
           ORDER BY trade_date DESC LIMIT 1""",
        (symbol, trade_date),
    ).fetchone()
    return row["source_note"] if row and row["source_note"] else None


def build_reversal_day1(conn, trade_date: str, top_n: int = 20) -> dict:
    """Build the separate Extreme Outflow -> Day-1 reversal research list.

    This bypasses C1+C2 candidate membership by design. It reads the full
    intraday snapshot universe and previous chip history, but always returns
    OBSERVE_ONLY while the rule remains research-only.
    """
    from .reversal import classify_reversal, reversal_summary
    from .rules import price_freshness

    rows = current_latest_rows(conn, trade_date)
    regime = market_regime(conn, trade_date)
    expected_chip = expected_chip_date(conn, trade_date)
    out = []
    for row in rows:
        flow = institutional_outflow_summary(conn, row["symbol"], trade_date)
        chip_fresh = bool(expected_chip and flow["chip_data_date"] == expected_chip)
        r5 = flow["institutional_net_5d_ratio"] if chip_fresh else None
        r20 = flow["institutional_net_20d_ratio"] if chip_fresh else None
        prior_outflow = any(v is not None and v < 0 for v in (r5, r20))
        extreme_outflow = bool(r5 is not None and r20 is not None and r5 < 0 and r20 < 0)
        stale_price = price_freshness(row["price_data_date"], trade_date) != "OK"
        price_change = row["price_change_pct"]
        price_reversal = bool(price_change is not None and float(price_change) >= 1.5)
        aflow = row["a_flow"]
        aflow_flip = bool(aflow is not None and float(aflow) > 0)
        above_vwap = bool(row["close"] is not None and row["vwap"] is not None and float(row["close"]) > float(row["vwap"]))
        persistence = aflow_persistence_metrics(conn, row["symbol"], trade_date)
        state = classify_reversal(
            prior_outflow=prior_outflow,
            extreme_outflow=extreme_outflow,
            price_reversal=price_reversal,
            aflow_flip=aflow_flip,
            aflow_persistence=persistence["aflow_persistence"],
            price_confirmation=persistence["price_confirmation"],
            above_vwap=above_vwap,
            stale_price=stale_price,
        )
        volume = float(row["volume"] or 0)
        aflow_ratio = (float(aflow) / volume) if aflow is not None and volume > 0 else None
        scenario = f"{state}__{'EXTREME_OUTFLOW' if extreme_outflow else 'OUTFLOW'}"
        card = {
            "symbol": row["symbol"], "name": row["stock_name"],
            "state": state, "action": "OBSERVE_ONLY", "scenario": scenario,
            "summary": reversal_summary(state),
            "control_note": _control_note(conn, row["symbol"], trade_date),
            "r1_prior_outflow": prior_outflow,
            "r1_extreme_outflow": extreme_outflow,
            "r2_price_reversal": price_reversal,
            "r3_aflow_flip": aflow_flip,
            "r4_aflow_persistence": persistence["aflow_persistence"],
            "r5_price_confirmation": persistence["price_confirmation"] and above_vwap,
            "above_vwap": above_vwap,
            "price_change_pct": row["price_change_pct"],
            "current_price": row["close"],
            "current_aflow": aflow,
            "aflow_ratio": aflow_ratio,
            "institutional_net_5d_ratio": r5,
            "institutional_net_20d_ratio": r20,
            "chip_data_date": flow["chip_data_date"],
            "chip_expected_date": expected_chip,
            "chip_stale_data": not chip_fresh,
            "price_data_date": row["price_data_date"],
            "flow_data_time": row["flow_data_time"] or row["ts"],
            "snapshot_time": row["as_of"],
            **persistence,
        }
        card["history"] = scenario_stats(conn, scenario, regime["regime"])
        out.append(card)

    priority = {
        "REVERSAL_PRIORITY": 0,
        "REVERSAL_DAY1_EARLY": 1,
        "OUTFLOW_REVERSAL_WATCH": 2,
        "STALE_PRICE_DATA": 3,
        "NOT_REVERSAL": 4,
    }
    out.sort(key=lambda x: (
        priority.get(x["state"], 9),
        -(float(x["price_change_pct"] or 0)),
        -(float(x["aflow_ratio"] or 0)),
    ))
    return {
        "trade_date": trade_date,
        "market_regime": regime["regime"],
        "research_only": True,
        "results": out[:top_n],
    }
