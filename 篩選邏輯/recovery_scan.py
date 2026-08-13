"""Independent rescue scan for stocks already classified as structural failures.

This module never changes the central five-state classification.  It only adds
an auditable recovery score and a T+1 confirmation rule beside rejected rows.
"""

from __future__ import annotations

from collections import defaultdict

import layered_score

HIGH_PRIORITY = 60
WATCH_MIN = 40


def _num(value):
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def sector_flow_turns(codes: list[str], code_group: dict[str, str],
                      current: dict[str, float], previous: dict[str, float]) -> dict[str, bool]:
    """Return per-code flags only when sector flow broadly turns positive.

    A sector must have at least two members, at least two thirds positive now,
    and no more than half positive previously. Missing values are not votes.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for code in codes:
        group = code_group.get(code)
        if group:
            groups[group].append(code)
    turned: dict[str, bool] = {}
    for members in groups.values():
        now = [_num(current.get(c)) for c in members]
        before = [_num(previous.get(c)) for c in members]
        now = [v for v in now if v is not None]
        before = [v for v in before if v is not None]
        hit = (len(now) >= 2 and len(before) >= 2 and
               sum(v > 0 for v in now) / len(now) >= 2 / 3 and
               sum(v > 0 for v in before) / len(before) <= 1 / 2)
        for code in members:
            turned[code] = hit
    return {code: turned.get(code, False) for code in codes}


def scan(classified: dict, features: dict) -> dict:
    """Score a rejected stock without mutating or replacing its classification."""
    eligible = classified.get("classification") == layered_score.TIER_REJECTED
    if not eligible:
        return {
            "eligible": False, "score": 0, "status": "非淘汰標的",
            "signals": [], "pending": [], "in_recovery_pool": False,
            "t1_trigger": "不適用",
        }

    close, low = _num(features.get("close")), _num(features.get("low"))
    open_price = _num(features.get("open"))
    ma5, ma20 = _num(features.get("ma5")), _num(features.get("ma20"))
    change = _num(features.get("change_rate"))
    total_net = _num(features.get("total_net"))
    flow_t = _num(features.get("aflow_today"))
    flow_y = _num(features.get("aflow_previous"))
    previous_low = _num(features.get("previous_low"))

    points = 0
    signals: list[str] = []
    pending: list[str] = []

    selling = ((total_net is not None and total_net < 0) or
               (flow_t is not None and flow_t < 0))
    if selling and change is not None and change >= -1.0:
        points += 25
        signals.append("價格抗跌／賣壓吸收 +25")
    elif change is None or (total_net is None and flow_t is None):
        pending.append("價格抗跌／賣壓吸收")

    if flow_y is not None and flow_t is not None:
        if flow_y < 0 < flow_t:
            points += 25
            signals.append("主動資金由負轉正 +25")
    else:
        pending.append("主動資金由負轉正")

    averages = [v for v in (ma5, ma20) if v is not None]
    if low is not None and close is not None and averages:
        if any(low < avg <= close for avg in averages):
            points += 20
            signals.append("尾盤收復 MA5／MA20 +20")
        elif (open_price is not None and open_price > low and
              low < min(averages) and close < min(averages) and
              (close - low) / (open_price - low) >= 0.60):
            # A true MA reclaim cannot coexist with the four rejection gates,
            # because those gates require the close to remain below MA5/20.
            # Preserve the T+1 reclaim as an upgrade condition while allowing
            # a strong late rebound below the averages to be rescued today.
            points += 20
            signals.append("尾盤明顯拉回 +20")
    else:
        pending.append("尾盤拉回／收復 MA5／MA20")

    if features.get("sector_flow_turn") is True:
        points += 15
        signals.append("族群資金同步轉強 +15")
    elif features.get("sector_flow_turn") is None:
        pending.append("族群資金同步轉強")

    if low is not None and previous_low is not None:
        if low >= previous_low:
            points += 10
            signals.append("今日低點未破前低 +10")
    else:
        pending.append("今日低點未破前低")

    if flow_y is not None and flow_t is not None:
        if flow_y < 0 and flow_t < 0 and abs(flow_t) <= abs(flow_y) * 0.70:
            points += 5
            signals.append("主動賣超縮小 +5")
    else:
        pending.append("主動賣超縮小")

    if points >= HIGH_PRIORITY:
        status = "🔄 高優先救援"
    elif points >= WATCH_MIN:
        status = "👀 淘汰觀察"
    else:
        status = "❌ 維持結構失效"
    return {
        "eligible": True, "score": points, "status": status,
        "signals": signals, "pending": sorted(set(pending)),
        "in_recovery_pool": points >= WATCH_MIN,
        "t1_trigger": "站回 MA5＋主動資金翻正＋突破淘汰日高點",
    }


def evaluate_t1_trigger(*, price, aflow, ma5, rejected_high) -> dict:
    """Recovery candidates may upgrade only after all three T+1 checks pass."""
    p, flow = _num(price), _num(aflow)
    avg, high = _num(ma5), _num(rejected_high)
    checks = {
        "站回 MA5": p is not None and avg is not None and p >= avg,
        "主動資金翻正": flow is not None and flow > 0,
        "突破淘汰日高點": p is not None and high is not None and p > high,
    }
    triggered = all(checks.values())
    return {
        "checks": checks, "triggered": triggered,
        "classification": layered_score.TIER_REVERSAL if triggered else layered_score.TIER_REJECTED,
    }
