"""Historical scenario statistics with explicit minimum-sample guards."""
from __future__ import annotations


def scenario_stats(conn, scenario: str, market_regime: str | None = None, min_n: int = 20) -> dict:
    where = "scenario = ?"
    args: list[object] = [scenario]
    if market_regime:
        where += " AND market_regime = ?"
        args.append(market_regime)
    row = conn.execute(
        f"""SELECT COUNT(*) n,
                   AVG(success) success_rate,
                   AVG(next_day_up) next_day_up_rate,
                   AVG(plus3) plus3_rate,
                   AVG(plus5) plus5_rate,
                   AVG(mfe) avg_mfe,
                   AVG(mae) avg_mae,
                   AVG(baseline_up_rate) baseline_up_rate
            FROM decision_history WHERE {where}""",
        args,
    ).fetchone()
    n = int(row["n"] or 0)
    success = float(row["success_rate"]) if row["success_rate"] is not None else None
    baseline = float(row["baseline_up_rate"]) if row["baseline_up_rate"] is not None else None
    delta = (success - baseline) if success is not None and baseline is not None else None
    return {
        "scenario": scenario,
        "market_regime": market_regime,
        "n": n,
        "sample_status": "OK" if n >= min_n else "INSUFFICIENT",
        "display_rate": round(success, 4) if n >= min_n and success is not None else None,
        "success_rate": success,
        "next_day_up_rate": float(row["next_day_up_rate"]) if row["next_day_up_rate"] is not None else None,
        "plus3_rate": float(row["plus3_rate"]) if row["plus3_rate"] is not None else None,
        "plus5_rate": float(row["plus5_rate"]) if row["plus5_rate"] is not None else None,
        "avg_mfe": float(row["avg_mfe"]) if row["avg_mfe"] is not None else None,
        "avg_mae": float(row["avg_mae"]) if row["avg_mae"] is not None else None,
        "baseline_up_rate": baseline,
        "baseline_delta": delta,
    }


def rescue_validation(conn, scenario_prefix: str = "%RESCUE_HIGH%", min_n: int = 60, min_delta: float = 0.15) -> dict:
    """Strict v4.1 validation: enough samples + excess rate in both regimes."""
    rows = conn.execute(
        """SELECT market_regime, COUNT(*) n, AVG(success) success_rate,
                  AVG(baseline_up_rate) baseline_rate
           FROM decision_history
           WHERE scenario LIKE ? AND market_regime IN ('RISK_ON','RISK_OFF')
           GROUP BY market_regime""",
        (scenario_prefix,),
    ).fetchall()
    by_regime = {}
    approved = True
    for regime in ("RISK_ON", "RISK_OFF"):
        row = next((r for r in rows if r["market_regime"] == regime), None)
        if row is None:
            by_regime[regime] = {"n": 0, "delta": None, "ok": False}
            approved = False
            continue
        n = int(row["n"] or 0)
        success = float(row["success_rate"] or 0)
        baseline = float(row["baseline_rate"] or 0)
        delta = success - baseline
        ok = n >= min_n and delta >= min_delta
        by_regime[regime] = {"n": n, "delta": delta, "ok": ok}
        approved = approved and ok
    return {"approved": approved, "by_regime": by_regime}
