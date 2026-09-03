"""Canonical field contract for MLS intraday price/volume/flow/chip metrics.

The contract intentionally keeps three concepts separate:
- volume_lots: total executed volume, directionless, live intraday.
- aflow_lots: active-buy minus active-sell volume, directional estimate, live intraday.
- institution_*: official institutional chip context, post-market / prior trading day.

A-flow is never called institutional buy/sell and is never inferred from total volume.
"""
from __future__ import annotations

from typing import Any

FIELD_LABELS = {
    "volume": "成交量",
    "aflow": "主動買賣差（A-flow）",
    "institution": "法人籌碼（前一交易日）",
}


def _num(value: Any):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(row: dict, *keys: str):
    for key in keys:
        if key in row and row.get(key) is not None:
            return row.get(key)
    return None


def normalize(row: dict | None) -> dict:
    """Normalize all intraday surfaces to one field contract.

    Never derive A-flow from total volume. If an explicit A-flow and the
    active-buy/active-sell reconstruction disagree materially, block the
    directional value instead of silently choosing one version.
    """
    row = dict(row or {})

    volume = _num(_first(row, "total_volume", "volume", "volume_lots"))
    if volume is not None:
        volume = int(round(volume))

    explicit_aflow = _num(_first(row, "aflow", "net_active", "aflow_lots"))
    active_buy = _num(_first(row, "active_buy", "buy_volume"))
    active_sell = _num(_first(row, "active_sell", "sell_volume"))
    derived_aflow = (active_buy - active_sell
                     if active_buy is not None and active_sell is not None else None)

    status = str(row.get("aflow_status") or "LIVE").upper()
    if (explicit_aflow is not None and derived_aflow is not None and
            abs(explicit_aflow - derived_aflow) > 1):
        aflow = None
        status = "CONFLICT"
    elif status in {"UNAVAILABLE", "BLOCKED", "DATA_BLOCKED", "CONFLICT"}:
        aflow = None
    else:
        aflow = explicit_aflow if explicit_aflow is not None else derived_aflow
        if aflow is not None:
            aflow = int(round(aflow))
        elif status == "LIVE":
            status = "NO_DATA"

    ratio = (aflow / volume * 100.0
             if aflow is not None and volume not in (None, 0) else None)

    institution_asof = _first(
        row, "chip_data_date", "institution_data_date", "chip_source_date", "source_date"
    )
    institution_label = _first(row, "institution_label", "chip_label")
    institution_metric_label = (
        f"法人籌碼（截至 {institution_asof}）" if institution_asof
        else FIELD_LABELS["institution"]
    )

    return {
        "volume_lots": volume,
        "volume_label": FIELD_LABELS["volume"],
        "volume_unit": "張",
        "volume_is_intraday": True,
        "aflow_lots": aflow,
        "aflow_label": FIELD_LABELS["aflow"],
        "aflow_unit": "張",
        "aflow_ratio_pct": round(ratio, 1) if ratio is not None else None,
        "aflow_status": status,
        "aflow_is_intraday": True,
        "aflow_is_institutional": False,
        "institution_label": institution_label,
        "institution_asof": institution_asof,
        "institution_metric_label": institution_metric_label,
        "institution_is_intraday": False,
        "institution_source_kind": "official_postmarket_cache",
        "field_contract_version": "intraday-metrics-v1",
    }
