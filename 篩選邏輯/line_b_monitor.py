"""Line B buy-point monitor presentation buckets.

This module deliberately does not change C1/C2 research qualification.  It
only classifies rows that are already available to the presentation layer.
"""

from __future__ import annotations

FLOW_CONFIRMED = frozenset(("OPEN_POSITIVE", "FLOW_FLIP"))
MONITOR_DISTANCE_PCT = 1.5

BUCKET_ORDER = {
    "PRICE_TRIGGERED": 0,
    "CONFIRMED": 1,
    "APPROACHING": 2,
    "WAITING_FUNDS": 3,
    "DISCOVERY": 4,
    "FAILED": 5,
}

BUCKET_LABELS = {
    "PRICE_TRIGGERED": "PRICE TRIGGER 已發生",
    "CONFIRMED": "A-flow 已確認",
    "APPROACHING": "接近確認",
    "WAITING_FUNDS": "等待資金",
    "DISCOVERY": "盤中發現",
    "FAILED": "失敗／轉弱",
}

STATUS_BY_BUCKET = {
    "PRICE_TRIGGERED": "PRICE_TRIGGERED",
    "CONFIRMED": "CONFIRMED",
    "APPROACHING": "WATCH_CLOSELY",
    "WAITING_FUNDS": "WAIT",
    "DISCOVERY": "DISCOVERY",
    "FAILED": "FAILED",
}


def _distance(row: dict):
    exp = row.get("explain") or {}
    value = exp.get("distance_pct", row.get("distance_pct"))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify(row: dict, is_eod: bool = False) -> str:
    """Classify a row for the monitor without changing C1/C2 qualification.

    ``source`` remains the audit/research provenance.  ``MONITOR_ONLY`` is
    intentionally a presentation-only source used for rows that were not a
    frozen C1+C2 candidate.
    """
    source = row.get("source")
    flow_class = row.get("flow_class")

    # Discovery provenance wins over the other labels: it must remain visibly
    # separate from the prior-night C1+C2 cohort.
    if source == "INTRADAY_DISCOVERY":
        return "DISCOVERY"
    distance = _distance(row)
    if (distance is not None and distance >= 0 and
            (bool(row.get("watch_mode_activated") or flow_class in FLOW_CONFIRMED))):
        return "PRICE_TRIGGERED"
    if bool(row.get("watch_mode_activated")) or flow_class in FLOW_CONFIRMED:
        return "CONFIRMED"
    if is_eod and source == "C1C2_PASS" and flow_class == "NO_FLIP":
        return "FAILED"

    near = distance is not None and distance >= -MONITOR_DISTANCE_PCT
    if near and bool(row.get("flow_improving")):
        return "APPROACHING"

    # A row is structurally monitorable when it passed C1, or when it is an
    # existing C1+C2 ledger row.  C2 alone is deliberately not promoted to a
    # structure signal because C1 is the frozen structure definition.
    if bool(row.get("c1_structure_intact")) or source == "C1C2_PASS":
        return "WAITING_FUNDS"
    return ""
