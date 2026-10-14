"""Fast Turn Watch v2 — independent, research-only 1–2 day candidate engine.

No production order actions, no edits to frozen Line A/Line B, no probabilities.
Each result carries observable reasons and data-quality failures.
All price/volume/flow thresholds are hypotheses until walk-forward validated.
"""
from __future__ import annotations

from datetime import datetime
from math import isfinite

VERSION = "fast_turn_candidate_v2_2026-10-14"
DISCOVERY_ONLY = "DISCOVERY ONLY"
MAX_PRICE = 1000.0
MAX_QUOTE_AGE_SECONDS = 120
MAX_SECTOR_AGE_SECONDS = 180

def _number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if isfinite(n) else None
    except (TypeError, ValueError):
        return None

def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None

def _fresh(value, now, max_age):
    t = _timestamp(value)
    ref = _timestamp(now)
    if t is None or ref is None:
        return False
    age = (ref - t).total_seconds()
    return 0 <= age <= max_age

def _result(status, signals=None, reasons=None):
    return {
        "status": status,
        "signals": signals or [],
        "reasons": reasons or [],
        "version": VERSION,
        "evidence_status": DISCOVERY_ONLY,
        "trade_approved": False,
    }

def discover_eod(row):
    """T-1 end-of-day discovery: no next-session data is accepted or consulted.

    Inputs are point-in-time daily facts. Prior leaders and flows are OPTIONAL
    evidence; no consecutive institutional-buy requirement and no MA20 gate.
    """
    date = row.get("data_date")
    keys = ("close", "prev_close", "high", "low", "volume")
    vals = {key: _number(row.get(key)) for key in keys}
    close, prev, high, low, volume = (vals[k] for k in keys)
    if (not date or not row.get("code") or any(v is None for v in vals.values())
            or prev <= 0 or close <= 0 or volume <= 0 or high <= low
            or not low <= close <= high):
        return _result("NO_DATA", reasons=["MISSING_OR_INVALID_TMINUS1_FACTS"])
    if close > MAX_PRICE:
        return _result("EXCLUDED", reasons=["PRICE_ABOVE_1000"])

    change = (close / prev - 1) * 100
    location = (close - low) / (high - low)
    vol_ma = _number(row.get("vol_ma5"))
    vol_ratio = volume / vol_ma if vol_ma and vol_ma > 0 else None
    signals = []
    if change <= -5 and location >= 0.45:
        signals.append("SHOCK_ABSORPTION")
    if low / prev <= 0.97 and location >= 0.7 and -4 <= change <= 2:
        signals.append("LONG_LOWER_SHADOW")
    foreign_net = _number(row.get("institutional_net"))
    if (str(row.get("institutional_date") or "") == str(date)
            and foreign_net is not None and foreign_net > 0
            and -2 <= change <= 1.5 and vol_ratio is not None and vol_ratio >= 0.8):
        signals.append("PRICE_FLOW_DIVERGENCE")
    prior_lead = _number(row.get("prior_five_day_return_pct"))
    if prior_lead is not None and prior_lead >= 7 and -5 <= change <= 0 and location >= 0.6:
        signals.append("LEADER_RETEST")
    sector_now = _number(row.get("sector_breadth"))
    sector_prev = _number(row.get("sector_breadth_previous"))
    sector_return = _number(row.get("sector_change_pct"))
    if (str(row.get("sector_data_date") or "") == str(date)
            and sector_now is not None and sector_prev is not None
            and sector_now >= 0.55 and sector_prev < 0.5
            and sector_return is not None and sector_return > 0
            and -2 <= change <= 2):
        signals.append("SECTOR_NEW_TURN")

    reasons = []
    if row.get("institutional_net") is not None and str(row.get("institutional_date")) != str(date):
        reasons.append("INSTITUTIONAL_DATA_NOT_ASOF_TMINUS1_IGNORED")
    if row.get("sector_breadth") is not None and str(row.get("sector_data_date")) != str(date):
        reasons.append("SECTOR_DATA_NOT_ASOF_TMINUS1_IGNORED")
    if row.get("market_regime") == "RISK_OFF":
        reasons.append("RISK_OFF_RESEARCH_ONLY")
    result = _result("WATCH" if signals else "NO_SETUP", signals=signals, reasons=reasons)
    result.update({"code": str(row["code"]), "data_date": str(date),
                   "tminus1_high": high, "tminus1_low": low,
                   "tminus1_close": close, "change_pct": round(change, 3)})
    return result

def intraday_review(candidate, quote, now):
    """A quote-trigger check, not a buy signal. All data needed MUST be live.

    Confirmations: price reclaims live VWAP and open from a low, OR breaks the
    T-1 high. Positive A-flow denotes aggressive trade direction only.
    """
    if candidate.get("status") != "WATCH":
        return _result("NOT_ELIGIBLE", reasons=["NO_TMINUS1_WATCH"])
    if not _fresh(quote.get("quote_ts"), now, MAX_QUOTE_AGE_SECONDS):
        return _result("NO_DATA", reasons=["STALE_OR_MISSING_QUOTE"])
    if str(quote.get("data_date")) != str(_timestamp(now).date()):
        return _result("NO_DATA", reasons=["QUOTE_WRONG_TRADING_DATE"])
    if not _fresh(quote.get("aflow_ts"), now, MAX_QUOTE_AGE_SECONDS):
        return _result("NO_DATA", reasons=["AFLOW_NOT_LIVE"])
    if not _fresh(quote.get("sector_ts"), now, MAX_SECTOR_AGE_SECONDS):
        return _result("NO_DATA", reasons=["SECTOR_NOT_LIVE"])
    required = ("price", "open", "vwap", "session_low", "change_pct",
                "aflow", "relative_volume", "sector_change_pct",
                "sector_advancing_ratio", "resistance")
    facts = {k: _number(quote.get(k)) for k in required}
    if (any(v is None for v in facts.values()) or
            not 0 < facts["price"] <= MAX_PRICE or facts["open"] <= 0 or
            facts["session_low"] <= 0 or facts["relative_volume"] <= 0 or
            not 0 <= facts["sector_advancing_ratio"] <= 1):
        return _result("NO_DATA", reasons=["INCOMPLETE_LIVE_CONFIRMATION"])

    p = facts["price"]
    if (p < candidate["tminus1_low"] or facts["session_low"] < candidate["tminus1_low"]\n            or quote.get("market_regime") == "RISK_OFF"):
        return _result("INVALIDATED" if (p < candidate["tminus1_low"] or facts["session_low"] < candidate["tminus1_low"]) else "RISK_BLOCK",
                       reasons=["PREVIOUS_LOW_BROKEN" if (p < candidate["tminus1_low"] or facts["session_low"] < candidate["tminus1_low"])
                                else "MARKET_RISK_OFF"])
    if facts["change_pct"] >= 6 or p >= facts["resistance"]:
        return _result("NO_CHASE", reasons=["EXTENDED_OR_AT_RESISTANCE"])
    sector_ok = (facts["sector_change_pct"] > 0 and
                 facts["sector_advancing_ratio"] >= 0.55)
    flow_ok = facts["aflow"] > 0
    samples = _number(quote.get("positive_observations"))
    sustained = samples is not None and samples >= 2
    rebound = (p >= facts["vwap"] and p >= facts["open"]
               and p >= facts["session_low"] * 1.012)
    breakout = p > candidate["tminus1_high"]
    volume_ok = facts["relative_volume"] >= 1.1
    if not (sector_ok and flow_ok and sustained and volume_ok and (rebound or breakout)):
        missing = []
        if not sector_ok: missing.append("SECTOR_NOT_CONFIRMED")
        if not flow_ok: missing.append("AFLOW_NOT_POSITIVE")
        if not sustained: missing.append("NEEDS_SECOND_LIVE_OBSERVATION")
        if not volume_ok: missing.append("LIVE_VOLUME_NOT_ACCELERATING")
        if not (rebound or breakout): missing.append("PRICE_REVERSAL_NOT_CONFIRMED")
        return _result("WATCH", candidate.get("signals"), missing)
    stop = max(candidate["tminus1_low"], facts["session_low"])
    risk = p - stop
    reward = facts["resistance"] - p
    if risk <= 0 or reward / risk < 1.5:
        return _result("NO_CHASE", reasons=["INSUFFICIENT_REWARD_TO_RISK"])
    result = _result("READY_FOR_MANUAL_REVIEW", candidate.get("signals"),
                     ["LIVE_REVERSAL_CONFIRMED", "RR_AT_LEAST_1_5"])
    result.update({"entry_reference": p, "invalidation_reference": stop,
                   "resistance_reference": facts["resistance"],
                   "reward_to_risk": round(reward / risk, 2)})
    return result
