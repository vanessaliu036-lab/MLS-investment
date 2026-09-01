"""Read-only bridge from the live MLS API into Flow × Chips v4.1 cards.

No writes to the production MLS. Data contract:
- intraday A-flow/price: Shioaji live row fields
- prior foreign chips: pre_activation.foreign_net_* with explicit source/date
"""
from __future__ import annotations

from urllib.request import Request, urlopen
import json

DEFAULT_URL = "http://66.42.42.150:8000/api/intraday-watchpool"


def fetch_live_rows(url: str = DEFAULT_URL, timeout: int = 30) -> dict:
    req = Request(url, headers={"User-Agent": "MLS-v4.1-flow-chips-preview/1.0"})
    with urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data.get("ok") or not isinstance(data.get("rows"), list):
        raise RuntimeError("live MLS payload missing ok/rows")
    return data


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _live_card(row: dict) -> dict:
    pa = row.get("pre_activation") or {}
    price = _num(row.get("price"))
    avg = _num(row.get("avg_price"))
    aflow = _num(row.get("aflow"))
    total = _num(row.get("total_volume"))
    change = _num(row.get("change_rate"))
    f_d = _num(pa.get("foreign_net_d"))
    f_5 = _num(pa.get("foreign_net_5d"))
    f_20 = _num(pa.get("foreign_net_20d"))
    above_avg = bool(price is not None and avg is not None and price > avg)
    aflow_ratio = (aflow / total) if aflow is not None and total and total > 0 else None
    prior_outflow = bool((f_5 is not None and f_5 < 0) or (f_20 is not None and f_20 < 0))
    extreme_outflow = bool(f_5 is not None and f_20 is not None and f_5 < 0 and f_20 < 0)
    five_positive = bool(f_5 is not None and f_5 > 0)
    twenty_negative = bool(f_20 is not None and f_20 < 0)
    price_reversal = bool(change is not None and change >= 1.5)
    price_weak = bool(change is not None and change < 0)
    extended = bool(pa.get("do_not_chase") or row.get("is_limit_up"))

    # Reversal track: persistence is intentionally NO_DATA because this endpoint
    # exposes the latest snapshot, not a 30-90 minute history series.
    if twenty_negative and five_positive and price_weak and not above_avg:
        reversal_state = "PREVIOUS_FLOW_REVERSAL_DAY2_3_FAILURE"
        reversal_reasons = ["REVERSAL_ALREADY_OCCURRED", "PRICE_CONTINUATION_FAILED", "BELOW_VWAP"]
    elif prior_outflow and price_weak and not above_avg:
        reversal_state = "OUTFLOW_WATCH_NOT_TRIGGERED"
        reversal_reasons = ["PRICE_NOT_REVERSED", "BELOW_VWAP", "NO_DAY1_TRIGGER"]
    elif prior_outflow and price_reversal and aflow is not None and aflow > 0 and above_avg:
        reversal_state = "REVERSAL_DAY1_EARLY_EXTENDED" if extended else "REVERSAL_DAY1_EARLY"
        reversal_reasons = ["A_FLOW_FLIPPED", "ABOVE_VWAP", "PRICE_REVERSED", "PERSISTENCE_NO_DATA"]
        if extended:
            reversal_reasons.append("DO_NOT_CHASE")
    elif prior_outflow and price_reversal and aflow is not None and aflow > 0 and not above_avg:
        reversal_state = "REVERSAL_ATTEMPT_NOT_ACCEPTED"
        reversal_reasons = ["A_FLOW_FLIPPED", "PRICE_REVERSED", "BELOW_VWAP", "PRICE_NOT_ACCEPTED"]
    elif prior_outflow:
        reversal_state = "OUTFLOW_REVERSAL_WATCH"
        reversal_reasons = ["REVERSAL_NOT_CONFIRMED"]
    else:
        reversal_state = "NOT_REVERSAL"
        reversal_reasons = ["NO_PRIOR_OUTFLOW"]

    # Flow tab conclusion.
    if aflow is None:
        flow_state, action, reasons = "NO_DATA", "OBSERVE_ONLY", ["A_FLOW_NO_DATA"]
    elif aflow > 0:
        if extended:
            flow_state, action, reasons = "STRONG_BUT_EXTENDED", "NO_CHASE", ["A_FLOW_POSITIVE", "EXTENSION_RISK_HIGH"]
        elif prior_outflow and price_reversal and above_avg:
            flow_state, action, reasons = "REVERSAL_DAY1_EARLY", "WATCH_PRIORITY", ["A_FLOW_POSITIVE", "PRIOR_OUTFLOW", "PRICE_ACCEPTED"]
        elif not above_avg:
            flow_state, action, reasons = "FLOW_POSITIVE_PRICE_NOT_ACCEPTED", "WAIT", ["A_FLOW_POSITIVE", "BELOW_VWAP"]
        elif (f_5 or 0) > 0 and (f_20 or 0) > 0:
            flow_state, action, reasons = "FLOW_CHIP_RESONANCE", "WATCH", ["A_FLOW_POSITIVE", "PRIOR_CHIPS_POSITIVE", "ABOVE_VWAP"]
        else:
            flow_state, action, reasons = "FLOW_POSITIVE", "WATCH", ["A_FLOW_POSITIVE"]
    else:
        if (f_5 is not None and f_5 > 0) or (f_20 is not None and f_20 > 0):
            flow_state, action, reasons = "STRONG_CHIP_INTRADAY_OUTFLOW", "NO_ENTRY", ["A_FLOW_NEGATIVE", "PRIOR_CHIPS_POSITIVE"]
        else:
            flow_state, action, reasons = "OUTFLOW_WEAK", "NO_ENTRY", ["A_FLOW_NEGATIVE", "PRIOR_CHIPS_NONPOSITIVE"]
        if not above_avg:
            reasons.append("BELOW_VWAP")
        if price_weak:
            reasons.append("PRICE_WEAK")

    return {
        "symbol": str(row.get("code") or ""),
        "name": row.get("name"),
        "sector": row.get("sector"),
        "price": price,
        "high": _num(row.get("high")),
        "low": _num(row.get("low")),
        "avg_price": avg,
        "change_rate": change,
        "aflow": aflow,
        "aflow_ratio": aflow_ratio,
        "total_volume": total,
        "flow_state": flow_state,
        "action": action,
        "reason_codes": reasons,
        "reversal_state": reversal_state,
        "reversal_reason_codes": reversal_reasons,
        "reversal_persistence": "NO_DATA",
        "above_vwap_proxy": above_avg,
        "is_limit_up": bool(row.get("is_limit_up")),
        "quadrant": row.get("quadrant"),
        "entry_status": row.get("entry_status"),
        "foreign_net_d": f_d,
        "foreign_net_5d": f_5,
        "foreign_net_20d": f_20,
        "foreign_days": _num(pa.get("foreign_days")),
        "foreign_source": pa.get("foreign_source"),
        "foreign_source_date": pa.get("foreign_source_date"),
        "ma5_distance_pct": _num(pa.get("ma5_distance_pct")),
        "volume_ratio": _num(pa.get("volume_ratio")),
        "do_not_chase": bool(pa.get("do_not_chase")),
        "price_source": row.get("price_source"),
        "quote_status": row.get("quote_status"),
        "aflow_status": row.get("aflow_status"),
    }


def build_live_view(payload: dict, top_n: int = 10) -> dict:
    cards = [_live_card(r) for r in payload.get("rows", [])]
    inflow = sorted((c for c in cards if (c["aflow"] or 0) > 0), key=lambda c: c["aflow"], reverse=True)[:top_n]
    outflow = sorted((c for c in cards if (c["aflow"] or 0) < 0), key=lambda c: c["aflow"])[:top_n]

    reversal_priority = {
        "REVERSAL_DAY1_EARLY": 0,
        "REVERSAL_DAY1_EARLY_EXTENDED": 1,
        "REVERSAL_ATTEMPT_NOT_ACCEPTED": 2,
        "PREVIOUS_FLOW_REVERSAL_DAY2_3_FAILURE": 3,
        "OUTFLOW_WATCH_NOT_TRIGGERED": 4,
        "OUTFLOW_REVERSAL_WATCH": 5,
        "NOT_REVERSAL": 9,
    }
    reversal = sorted(
        cards,
        key=lambda c: (
            reversal_priority.get(c["reversal_state"], 8),
            -(c["change_rate"] or 0),
            -(c["aflow_ratio"] or 0),
        ),
    )
    # Keep meaningful reversal/control rows; fallback to all if none.
    meaningful = [c for c in reversal if c["reversal_state"] != "NOT_REVERSAL"]
    reversal = (meaningful or reversal)[:20]

    return {
        "source": payload.get("source"),
        "snapshot": payload.get("snapshot"),
        "updated_at": payload.get("updated_at"),
        "read_only": payload.get("read_only"),
        "inflow": inflow,
        "outflow": outflow,
        "reversal": reversal,
    }
