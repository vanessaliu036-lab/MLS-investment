"""Independent Early Activation candidate classifier.

This module is research-only.  It deliberately does not import Opportunity
scoring or emit a score/probability.  See ``early_activation_research.md``.
"""
from __future__ import annotations

import math
import statistics
from typing import Iterable, Optional

import pre_activation as pa

NEW_TURN = "NEW_TURN"
RECONFIRM = "RECONFIRM"
ACCUMULATION_RETEST = "ACCUMULATION_RETEST"

RISK_ON = "RISK_ON"
TURNING_POSITIVE = "TURNING_POSITIVE"
NEUTRAL = "NEUTRAL"

DISCOVERY_ONLY = "DISCOVERY ONLY"
RULE_VERSION = "early_activation_candidate_v1_2026-08-26"


def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _distance_pct(row: dict) -> Optional[float]:
    direct = _num(row.get("ma5_distance_pct"))
    if direct is not None:
        return direct
    close, ma5 = _num(row.get("close")), _num(row.get("ma5"))
    if close is None or not ma5:
        return None
    return (close / ma5 - 1.0) * 100.0


def _volume_ratio(row: dict) -> Optional[float]:
    direct = _num(row.get("volume_ratio"))
    if direct is not None:
        return direct
    volume, average = _num(row.get("volume")), _num(row.get("vol_ma20"))
    if volume is None or not average:
        return None
    return volume / average


def _newest_first(rows: Iterable[dict]) -> list[dict]:
    values = [dict(row) for row in rows]
    if values and all(row.get("data_date") for row in values):
        return sorted(values, key=lambda row: str(row["data_date"]), reverse=True)
    return values


def sector_context(row: dict) -> str:
    """Map production sector facts to the three research contexts.

    ``RISK_OFF`` is handled separately by eligibility and is never relabelled as
    positive.  Returning NEUTRAL here keeps the context vocabulary fixed while
    the exclusion reason preserves the original risk fact.
    """
    regime = row.get("sector_regime")
    if regime == RISK_ON:
        return RISK_ON
    if regime == "RISK_OFF":
        return NEUTRAL
    sector_return = _num(row.get("sector_ret_median"))
    breadth = _num(row.get("sector_breadth"))
    if sector_return is not None and sector_return > 0 and breadth is not None and breadth >= 50:
        return TURNING_POSITIVE
    return NEUTRAL


def _common_rejection(t0: dict) -> list[str]:
    foreign_days = _num(t0.get("foreign_days"))
    distance = _distance_pct(t0)
    volume_ratio = _volume_ratio(t0)
    has_sector = (t0.get("sector_regime") is not None or
                  (_num(t0.get("sector_ret_median")) is not None and
                   _num(t0.get("sector_breadth")) is not None))
    if foreign_days is None or distance is None or volume_ratio is None or not has_sector:
        return ["MISSING_REQUIRED_FACTS"]
    if t0.get("sector_regime") == "RISK_OFF":
        return ["SECTOR_RISK_OFF"]
    if abs(distance) > pa.MA5_NEAR * 100:
        return ["PRICE_NOT_NEAR_MA5"]
    if volume_ratio >= pa.VOL_RISING:
        return ["VOLUME_ALREADY_ACTIVE"]
    return []


def _is_accumulation_retest(current_days: int, history: list[dict]) -> bool:
    if current_days < pa.FOREIGN_VERY_STRONG or len(history) < 4:
        return False
    # T0 plus the newest four prior sessions must still belong to one positive run.
    if any((_num(row.get("foreign_days")) or 0) <= 0 for row in history[:4]):
        return False
    prior_distances = [_distance_pct(row) for row in history[:5]]
    return any(distance is not None and distance >= pa.MA5_HOT * 100
               for distance in prior_distances)


def _is_reconfirm(current_days: int, history: list[dict]) -> bool:
    if current_days < pa.FOREIGN_STRONG_DAYS:
        return False
    # Remove the prior days that are part of the current uninterrupted run.
    before_run = history[max(current_days - 1, 0):5]
    if len(before_run) < 2:
        return False
    seen_positive = False
    interrupted_after_positive = False
    for row in reversed(before_run):  # oldest -> newest
        streak = _num(row.get("foreign_days"))
        if streak is None:
            continue
        if streak > 0:
            seen_positive = True
        elif seen_positive:
            interrupted_after_positive = True
    return seen_positive and interrupted_after_positive


def _is_new_turn(current_days: int, history: list[dict]) -> bool:
    if current_days not in (pa.FOREIGN_STRONG_DAYS, pa.FOREIGN_STRONG_DAYS + 1):
        return False
    before_run_index = current_days - 1
    if len(history) <= before_run_index:
        return False
    before_run = _num(history[before_run_index].get("foreign_days"))
    return before_run is not None and before_run < pa.FOREIGN_STRONG_DAYS


def classify(t0: dict, prior_days: Iterable[dict]) -> dict:
    """Classify one T0 row from facts available no later than T0 close.

    ``prior_days`` may include at most five prior trading sessions.  If rows have
    ``data_date`` they are sorted newest-first; otherwise newest-first is assumed.
    """
    history = _newest_first(prior_days)[:5]
    context = sector_context(t0)
    output = {
        "setup_type": None,
        "sector_context": context,
        "evidence_status": DISCOVERY_ONLY,
        "rule_version": RULE_VERSION,
        "reasons": [],
    }
    rejected = _common_rejection(t0)
    if rejected:
        output["reasons"] = rejected
        return output

    current = int(_num(t0.get("foreign_days")))
    if _is_accumulation_retest(current, history):
        output["setup_type"] = ACCUMULATION_RETEST
        output["reasons"] = ["FOREIGN_ACCUMULATION_INTACT", "PRICE_RECONVERGED_TO_MA5"]
    elif _is_reconfirm(current, history):
        output["setup_type"] = RECONFIRM
        output["reasons"] = ["PRIOR_POSITIVE_INTERRUPTED", "FOREIGN_BUYING_RESUMED"]
    elif _is_new_turn(current, history):
        output["setup_type"] = NEW_TURN
        output["reasons"] = ["FRESH_FOREIGN_BUY_STREAK"]
    else:
        output["reasons"] = ["NO_SETUP_PATTERN"]
    return output


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def discovery_metrics(values: Iterable[float]) -> dict:
    valid = [_num(value) for value in values]
    valid = [value for value in valid if value is not None]
    if not valid:
        return {"n": 0, "hit_plus_3_rate": None, "mean_return_pct": None,
                "p50_return_pct": None, "p90_return_pct": None,
                "non_up_rate": None}
    n = len(valid)
    return {
        "n": n,
        "hit_plus_3_rate": round(sum(value >= 3.0 for value in valid) / n * 100, 2),
        "mean_return_pct": round(statistics.mean(valid), 2),
        "p50_return_pct": round(_percentile(valid, 0.50), 2),
        "p90_return_pct": round(_percentile(valid, 0.90), 2),
        "non_up_rate": round(sum(value <= 0 for value in valid) / n * 100, 2),
    }


def evaluate(rows: Iterable[dict]) -> dict:
    """Compare setup cells with no-setup rows on the same dates and context."""
    valid_rows = [dict(row) for row in rows if _num(row.get("t1_return_pct")) is not None]
    cells = sorted({(row.get("setup_type"), row.get("sector_context"))
                    for row in valid_rows if row.get("setup_type")})
    by_cell = []
    for setup_type, context in cells:
        setup_rows = [row for row in valid_rows
                      if row.get("setup_type") == setup_type and
                      row.get("sector_context") == context]
        dates = {str(row.get("data_date")) for row in setup_rows}
        baseline_rows = [row for row in valid_rows
                         if not row.get("setup_type") and
                         row.get("sector_context") == context and
                         str(row.get("data_date")) in dates]
        by_cell.append({
            "setup_type": setup_type,
            "sector_context": context,
            "metrics": discovery_metrics(row["t1_return_pct"] for row in setup_rows),
            "matched_baseline": discovery_metrics(
                row["t1_return_pct"] for row in baseline_rows),
        })
    setup_rows = [row for row in valid_rows if row.get("setup_type")]
    return {
        "evidence_status": DISCOVERY_ONLY,
        "rule_version": RULE_VERSION,
        "by_setup_context": by_cell,
        "overall_setup": discovery_metrics(row["t1_return_pct"] for row in setup_rows),
        "conclusion_allowed": False,
    }

