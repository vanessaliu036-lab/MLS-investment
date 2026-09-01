"""Read-only bridge from the live MLS API into Reversal Lab.

This module does not change the production Trend / Entry model.
Data contract:
- intraday A-flow/price: Shioaji live row fields
- prior foreign chips: pre_activation.foreign_net_* with explicit source/date
- persistence: never inferred from one snapshot; remains NO_DATA until the lab
  has multiple time-stamped observations.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from urllib.request import Request, urlopen
import json

DEFAULT_URL = "http://66.42.42.150:8000/api/intraday-watchpool"


def fetch_live_rows(url: str = DEFAULT_URL, timeout: int = 30) -> dict:
    req = Request(url, headers={"User-Agent": "MLS-Reversal-Lab/1.0"})
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


def _meaningful_recent_flip(f5, f20) -> bool:
    """A tiny positive 5D value must not erase a still-large 20D outflow.

    Treat the recent flow as a real prior reversal only when it has repaired at
    least 10% of the negative 20D balance, or the 5D net itself is substantial.
    This keeps 3026 (+110 vs -11,047) in OUTFLOW WATCH while 8358
    (+7,146 vs -19,567) is a genuine previous-flow-reversal control.
    """
    if f5 is None or f20 is None or f5 <= 0 or f20 >= 0:
        return False
    repair_ratio = f5 / abs(f20) if f20 else 0.0
    return repair_ratio >= 0.10 or f5 >= 1000


def _grade_reversal(change, aflow_ratio, is_limit_up) -> str | None:
    if change is None or aflow_ratio is None:
        return None
    if is_limit_up or (change >= 9.0 and aflow_ratio >= 0.20):
        return "A+"
    if change >= 3.0 and aflow_ratio >= 0.10:
        return "A"
    if change >= 3.0 and aflow_ratio >= 0.05:
        return "B+"
    return "B"


def _price_confirmation(price, avg, change) -> str:
    if price is None or avg is None:
        return "NO_DATA"
    if price >= avg:
        return "CONFIRMED"
    # A Day-1 does not disappear merely because the close finishes a few ticks
    # below VWAP. Within -0.5% is a weakened acceptance, not a failed trigger.
    if change is not None and change >= 1.5 and price >= avg * 0.995:
        return "WEAKENED"
    return "FAILED"


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
    above_avg = bool(price is not None and avg is not None and price >= avg)
    aflow_ratio = (aflow / total) if aflow is not None and total and total > 0 else None
    prior_outflow = bool((f_5 is not None and f_5 < 0) or (f_20 is not None and f_20 < 0))
    meaningful_recent_flip = _meaningful_recent_flip(f_5, f_20)
    trend_control = bool(f_5 is not None and f_20 is not None and f_5 > 0 and f_20 > 0)
    price_reversal = bool(change is not None and change >= 1.5)
    price_weak = bool(change is not None and change < 0)
    extended = bool(pa.get("do_not_chase") or row.get("is_limit_up"))
    price_confirmation = _price_confirmation(price, avg, change)

    lab_role = "OTHER_CONTROL"
    reversal_grade = None

    if meaningful_recent_flip and price_weak and not above_avg:
        lab_role = "REVERSAL_FAILURE_CONTROL"
        reversal_state = "PREVIOUS_FLOW_REVERSAL_DAY2_3_FAILURE"
        reversal_reasons = ["REVERSAL_ALREADY_OCCURRED", "PRICE_CONTINUATION_FAILED", "BELOW_VWAP"]
    elif prior_outflow and price_weak and not above_avg:
        lab_role = "OUTFLOW_WATCH"
        reversal_state = "OUTFLOW_WATCH_NOT_TRIGGERED"
        reversal_reasons = ["PRICE_NOT_REVERSED", "BELOW_VWAP", "NO_DAY1_TRIGGER"]
    elif (
        prior_outflow
        and price_reversal
        and aflow is not None
        and aflow > 0
        and price_confirmation in {"CONFIRMED", "WEAKENED"}
    ):
        lab_role = "REVERSAL_DAY1"
        reversal_grade = _grade_reversal(change, aflow_ratio, bool(row.get("is_limit_up")))
        reversal_state = "REVERSAL_DAY1_EARLY_EXTENDED" if extended else "REVERSAL_DAY1_EARLY"
        reversal_reasons = ["A_FLOW_FLIPPED", "PRICE_REVERSED", "PERSISTENCE_NO_DATA"]
        reversal_reasons.append("ABOVE_VWAP" if price_confirmation == "CONFIRMED" else "VWAP_ACCEPTANCE_WEAKENED")
        if extended:
            reversal_reasons.append("DO_NOT_CHASE")
    elif prior_outflow:
        lab_role = "OUTFLOW_WATCH"
        reversal_state = "OUTFLOW_REVERSAL_WATCH"
        reversal_reasons = ["REVERSAL_NOT_CONFIRMED"]
    else:
        lab_role = "TREND_CONTROL" if trend_control else "OTHER_CONTROL"
        reversal_state = "NOT_REVERSAL"
        reversal_reasons = ["NO_PRIOR_OUTFLOW"]

    # Flow tab conclusion remains independent from the formal Entry page.
    if aflow is None:
        flow_state, action, reasons = "NO_DATA", "OBSERVE_ONLY", ["A_FLOW_NO_DATA"]
    elif aflow > 0:
        if lab_role == "REVERSAL_DAY1":
            flow_state = "REVERSAL_DAY1_EARLY"
            action = "NO_CHASE" if extended else "WATCH_PRIORITY"
            reasons = ["A_FLOW_POSITIVE", "PRIOR_OUTFLOW", "PRICE_CONFIRMATION_" + price_confirmation]
            if extended:
                reasons.append("EXTENSION_RISK_HIGH")
        elif extended:
            flow_state, action, reasons = "STRONG_BUT_EXTENDED", "NO_CHASE", ["A_FLOW_POSITIVE", "EXTENSION_RISK_HIGH"]
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
        "lab_role": lab_role,
        "reversal_grade": reversal_grade,
        "reversal_state": reversal_state,
        "reversal_reason_codes": reversal_reasons,
        "flow_persistence": "NO_DATA",
        "reversal_persistence": "NO_DATA",
        "price_confirmation": price_confirmation,
        "above_vwap_proxy": above_avg,
        "day2_ready": "PENDING_PERSISTENCE" if lab_role == "REVERSAL_DAY1" else "N/A",
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


def _apply_sector_confirmation(cards: list[dict]) -> None:
    groups = defaultdict(list)
    for c in cards:
        if c.get("sector"):
            groups[c["sector"]].append(c)

    for members in groups.values():
        changes = [c["change_rate"] for c in members if c.get("change_rate") is not None]
        up_ratio = (sum(1 for v in changes if v > 0) / len(changes)) if changes else None
        flows = [c["aflow"] for c in members if c.get("aflow") is not None]
        flow_pos_ratio = (sum(1 for v in flows if v > 0) / len(flows)) if flows else None
        med = median(changes) if changes else None
        n = len(members)
        if n >= 2 and med is not None and med > 0 and (flow_pos_ratio or 0) >= 0.5:
            status = "CONFIRMED"
        elif (up_ratio or 0) >= 0.5 or (flow_pos_ratio or 0) >= 0.5:
            status = "PARTIAL"
        else:
            status = "WEAK"
        for c in members:
            c["sector_confirmation"] = status
            c["sector_member_count"] = n
            c["sector_up_ratio"] = up_ratio
            c["sector_aflow_positive_ratio"] = flow_pos_ratio
            c["sector_median_change"] = med


def build_live_view(payload: dict, top_n: int = 10) -> dict:
    cards = [_live_card(r) for r in payload.get("rows", [])]
    _apply_sector_confirmation(cards)

    inflow = sorted((c for c in cards if (c["aflow"] or 0) > 0), key=lambda c: c["aflow"], reverse=True)[:top_n]
    outflow = sorted((c for c in cards if (c["aflow"] or 0) < 0), key=lambda c: c["aflow"])[:top_n]

    role_priority = {
        "REVERSAL_DAY1": 0,
        "OUTFLOW_WATCH": 1,
        "REVERSAL_FAILURE_CONTROL": 2,
        "TREND_CONTROL": 3,
        "OTHER_CONTROL": 9,
    }
    grade_priority = {"A+": 0, "A": 1, "B+": 2, "B": 3, None: 9}
    reversal = sorted(
        cards,
        key=lambda c: (
            role_priority.get(c["lab_role"], 8),
            grade_priority.get(c.get("reversal_grade"), 9),
            -(c["change_rate"] or 0),
            -(c["aflow_ratio"] or 0),
        ),
    )
    meaningful = [c for c in reversal if c["lab_role"] != "OTHER_CONTROL"]
    reversal = (meaningful or reversal)[:20]

    return {
        "lab_name": "資金反轉驗證 / Reversal Lab",
        "model_scope": "FORWARD_TEST_ONLY",
        "source": payload.get("source"),
        "snapshot": payload.get("snapshot"),
        "updated_at": payload.get("updated_at"),
        "read_only": payload.get("read_only"),
        "inflow": inflow,
        "outflow": outflow,
        "reversal": reversal,
    }
