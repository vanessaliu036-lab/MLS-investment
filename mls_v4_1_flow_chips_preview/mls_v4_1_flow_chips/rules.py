"""Deterministic v4.1 rule primitives.

No I/O lives here. These functions are deliberately small so the same
logic can be backtested and used by the preview API without drift.
"""
from __future__ import annotations


def price_freshness(price_data_date: str | None, trade_date: str) -> str:
    """Return OK only when price data belongs to the requested trade date."""
    if not price_data_date or price_data_date != trade_date:
        return "STALE"
    return "OK"


def compute_clv(
    high: float | None,
    low: float | None,
    close: float | None,
    prev_close: float | None,
    min_range_pct: float = 0.025,
) -> tuple[float | None, str]:
    """Compute close-location value and its confidence flag.

    VALID requires an intraday range >= 2.5% of previous close. Smaller
    ranges are retained for display but must not be trusted for rescue.
    """
    if high is None or low is None or close is None:
        return None, "INVALID"
    rng = high - low
    if rng <= 0:
        return None, "INVALID"
    raw = max(0.0, min(1.0, (close - low) / rng))
    if not prev_close or prev_close <= 0:
        return raw, "LOW_CONFIDENCE"
    range_pct = rng / prev_close
    if range_pct < min_range_pct:
        return raw, "LOW_CONFIDENCE"
    return raw, "VALID"


def price_acceptance(
    clv: float | None,
    confidence: str,
    *,
    close: float | None,
    vwap: float | None,
    prev_close: float | None,
    rescue_threshold: float = 0.55,
) -> bool:
    """Price acceptance with the v4.1 low-range fallback."""
    if confidence == "VALID" and clv is not None:
        return clv >= rescue_threshold
    if close is None or vwap is None or prev_close is None:
        return False
    return close > vwap and close > prev_close


def volume_ratio(volume: float | None, ma5_volume: float | None) -> float | None:
    if volume is None or ma5_volume is None or ma5_volume <= 0:
        return None
    return volume / ma5_volume


def classify_volume_quality(
    clv: float | None,
    confidence: str,
    vol_ratio: float | None,
) -> str:
    """Return v4.1 2×2 price-acceptance × volume-quality state."""
    if confidence != "VALID" or clv is None or vol_ratio is None:
        return "UNKNOWN"

    if clv >= 0.75:
        if vol_ratio < 0.8:
            return "SHAKEOUT"
        if vol_ratio > 1.5:
            return "HEAVY_ABSORPTION"
        return "ACCEPTED_NORMAL_VOLUME"

    if clv < 0.55:
        if vol_ratio < 0.8:
            return "NATURAL_DECAY"
        if vol_ratio > 1.5:
            return "FLOW_PRICE_DIVERGENCE"
        return "REJECTED_NORMAL_VOLUME"

    return "MIXED"
