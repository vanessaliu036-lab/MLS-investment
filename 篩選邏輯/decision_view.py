"""Canonical presentation model for stock classification and entry timing.

This module is deliberately pure.  It does not read databases and never
changes the four structural-failure gates.  Pipelines provide facts; this
module produces one consistent view for every API and UI.
"""

from __future__ import annotations

import layered_score


POOL_MAP = {
    layered_score.TIER_CORE: "core",
    layered_score.TIER_REVERSAL: "reversal",
    layered_score.TIER_NO_CHASE: "pullback",
    layered_score.TIER_CANDIDATE: "watch",
    layered_score.TIER_REJECTED: "rejected",
}


def _num(value):
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value):
    value = _num(value)
    if value is None:
        return "—"
    return f"{value:g}"


def margin_tag(margin: dict | None) -> str:
    """Financing is a chip fact, never a reason to enter."""
    row = margin or {}
    change = _num(row.get("margin_change"))
    balance = _num(row.get("margin_balance"))
    if change is None:
        return "融資 —"
    previous = balance - change if balance is not None else None
    if previous and previous > 0:
        shown = f"{change / previous * 100:+.1f}%"
    else:
        shown = f"{change:+g}"
    if change > 0:
        return f"融資：{shown} ⚠ 槓桿增加"
    if change < 0:
        return f"融資：{shown} 籌碼降槓桿"
    return "融資：0% 持平"


def ranking_factors(market: dict) -> dict:
    """Three change-sensitive diagnostics; institutional streak is excluded."""
    change = _num(market.get("change_rate"))
    flow = _num(market.get("aflow_today"))
    previous_flow = _num(market.get("aflow_previous"))
    close, high, low = (_num(market.get(k)) for k in ("close", "high", "low"))
    close_pos = ((close - low) / (high - low)
                 if None not in (close, high, low) and high > low else None)

    absorption = None
    selling = ((flow is not None and flow < 0) or
               (_num(market.get("total_net")) is not None and
                _num(market.get("total_net")) < 0))
    if selling and change is not None:
        damage = max(0.0, -change)
        resilience = max(0.0, 100.0 - damage * 25.0)
        position = (close_pos * 100.0) if close_pos is not None else 50.0
        absorption = round(resilience * 0.65 + position * 0.35, 1)

    buying = None
    if flow is not None and flow > 0 and change is not None:
        # Measures price response, not raw money size. Large inflow with little
        # movement is deliberately less efficient than a smaller decisive move.
        buying = round(min(100.0, max(0.0, change) * 25.0), 1)

    acceleration = None
    if flow is not None and previous_flow is not None:
        base = max(abs(previous_flow), abs(flow), 1.0)
        acceleration = max(0.0, min(100.0, (flow - previous_flow) / base * 50.0))
        prior_high = _num(market.get("prior_high"))
        ma20 = _num(market.get("ma20"))
        if close is not None and ((prior_high is not None and close > prior_high) or
                                  (ma20 is not None and close >= ma20)):
            acceleration = min(100.0, acceleration + 15.0)
        acceleration = round(acceleration, 1)

    available = [x for x in (absorption, buying, acceleration) if x is not None]
    score = round(sum(available) / len(available), 1) if available else None
    return {
        "pressure_absorption": absorption,
        "buying_efficiency": buying,
        "chip_reversal_acceleration": acceleration,
        "decision_rank_score": score,
        "ranking_pending": [name for name, value in (
            ("抗賣壓係數", absorption), ("買盤效率係數", buying),
            ("籌碼反轉加速度", acceleration)) if value is None],
    }


def _reason_tags(classified: dict, market: dict) -> list[str]:
    close = _num(market.get("close"))
    prior_high = _num(market.get("prior_high"))
    ma20 = _num(market.get("ma20"))
    low = _num(market.get("low"))
    flow, previous_flow = _num(market.get("aflow_today")), _num(market.get("aflow_previous"))
    sector_rel = _num(market.get("sector_rel"))
    change = _num(market.get("change_rate"))
    volume, vol_ma20 = _num(market.get("volume")), _num(market.get("vol_ma20"))
    tags = []
    if close is not None and prior_high is not None and close > prior_high:
        tags.append("突破前高")
    if previous_flow is not None and previous_flow < 0 and flow is not None and flow > 0:
        tags.append("主動資金翻正")
    if sector_rel is not None and sector_rel >= 0.5:
        tags.append("族群轉強")
    if low is not None and close is not None and ma20 is not None and low < ma20 <= close:
        tags.append("站回 MA20")
    if (change is not None and change > 0 and volume is not None and vol_ma20 and
            volume / vol_ma20 >= 1.2):
        tags.append("量價同步")
    if not tags:
        for reason in classified.get("reasons") or []:
            if "融資" not in str(reason):
                tags.append(str(reason).replace("價漲量增收盤穩", "量價同步"))
    return list(dict.fromkeys(tags))[:3]


def _next_upgrade(classification: str, market: dict) -> str:
    close = _num(market.get("current_price"))
    if close is None:
        close = _num(market.get("close"))
    prior_high, ma5, ma20 = (_num(market.get(k)) for k in ("prior_high", "ma5", "ma20"))
    flow = _num(market.get("aflow_today"))
    if classification == layered_score.TIER_CORE:
        return f"回測 MA5 {_fmt(ma5)} 不破 → 可進" if ma5 is not None else "等待低風險買點"
    if classification == layered_score.TIER_NO_CHASE:
        return f"回測 MA5 {_fmt(ma5)} 不破 → 可進" if ma5 is not None else "等待回測支撐 → 可進"
    if classification == layered_score.TIER_REVERSAL:
        if ma20 is not None and (close is None or close < ma20):
            return f"站回 MA20 {_fmt(ma20)} → A級"
        if flow is None or flow <= 0:
            return "主動資金翻正 → A級"
        if prior_high is not None and (close is None or close <= prior_high):
            return f"突破昨高 {_fmt(prior_high)} → A級"
        return "價格、資金、結構確認 → A級"
    if classification == layered_score.TIER_CANDIDATE:
        if ma20 is not None and (close is None or close < ma20):
            return f"站回 MA20 {_fmt(ma20)} → 反轉"
        if flow is None or flow <= 0:
            return "主動資金翻正 → 反轉"
        return f"突破昨高 {_fmt(prior_high)} → A級" if prior_high is not None else "突破壓力 → 反轉"
    return "維持結構失效；等待 Recovery Scan"


def _entry_state(classification: str, market: dict, trigger: dict) -> tuple[str, str, str]:
    price = _num(market.get("current_price"))
    if price is None:
        price = _num(market.get("close"))
    high = _num(market.get("high"))
    trigger_price = _num(trigger.get("trigger_price"))
    track = str(trigger.get("track") or "")
    no_chase = (classification == layered_score.TIER_NO_CHASE or
                bool(market.get("is_limit_up")))
    if no_chase:
        return "no_chase", "禁止追價", "C"
    if track in ("engine", "引擎軌") or classification == layered_score.TIER_NO_CHASE:
        return "wait_pullback", "等待回測", "B"
    if trigger_price is None or price is None:
        return "not_triggered", "尚未觸發", "C"
    if high is not None and high >= trigger_price and price < trigger_price:
        return "trigger_failed", "觸發後失敗", "D"
    if price >= trigger_price:
        return "triggered", "已觸發", "A"
    distance = (trigger_price - price) / trigger_price * 100
    if distance <= 1.0:
        return "near_trigger", f"距離觸發 {distance:.2f}%", "B"
    return "not_triggered", "尚未觸發", "C"


def build(classified: dict, market: dict | None, trigger: dict | None) -> dict:
    market = dict(market or {})
    trigger = dict(trigger or {})
    classification = classified.get("classification") or layered_score.TIER_CANDIDATE
    close = _num(market.get("close"))
    prior_high = _num(market.get("prior_high"))
    ma20 = _num(market.get("ma20"))
    flow = _num(market.get("aflow_today"))
    confirmed_reversal = (classification in (
                              layered_score.TIER_REVERSAL,
                              layered_score.TIER_NO_CHASE,
                              layered_score.TIER_CANDIDATE) and
                          None not in (close, prior_high, ma20, flow) and
                          close > prior_high and close >= ma20 and flow > 0)
    if confirmed_reversal:
        classification = layered_score.TIER_CORE

    state, state_label, quality = _entry_state(classification, market, trigger)
    factors = ranking_factors(market)
    margin_label = margin_tag(market)
    institution = market.get("institution_label") or "法人 —"
    flow_label = "資金 —" if flow is None else f"資金 {flow:+,.0f}"
    volume, vol_ma20 = _num(market.get("volume")), _num(market.get("vol_ma20"))
    volume_label = f"量能 {volume / vol_ma20:.1f}x" if volume is not None and vol_ma20 else "量能 —"
    result = {
        "classification": classification,
        "display_pool": POOL_MAP[classification],
        "potential_grade": "A" if confirmed_reversal else classified.get("potential_grade", "B"),
        "trend_stage": ("🔥 反轉確認" if confirmed_reversal else
                        (classified.get("trend_stage") or "未啟動")),
        "entry_quality": quality,
        "entry_state": state,
        "entry_state_label": state_label,
        "next_upgrade_condition": _next_upgrade(classification, market),
        "reason_tags": _reason_tags(classified, market),
        "chip_tags": [institution, flow_label, margin_label, volume_label],
        "institution_label": institution,
        "flow_label": flow_label,
        "margin_label": margin_label,
        "volume_label": volume_label,
    }
    result.update(factors)
    return result
