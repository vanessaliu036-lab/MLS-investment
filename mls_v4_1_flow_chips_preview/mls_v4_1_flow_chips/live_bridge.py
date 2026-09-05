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

from .reversal_state import ReversalStateMachine

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


def build_participation_judgment(card: dict) -> dict:
    """Decide whether the current move is still tradable.

    Risk markers change sizing or entry style; they do not veto a move by
    themselves.  Only a concrete combination of negative flow and failed
    price acceptance is labelled exhaustion/failure.
    """
    aflow = _num(card.get("aflow"))
    ratio = _num(card.get("aflow_ratio"))
    change = _num(card.get("change_rate"))
    above = card.get("above_vwap_proxy")
    volume_ratio = _num(card.get("volume_ratio"))
    gap = _num(card.get("ma5_distance_pct"))
    price = _num(card.get("price"))
    key_price = _num(card.get("line_b_key_price"))

    positive_flow = aflow is not None and aflow > 0
    negative_flow = aflow is not None and aflow < 0
    positive_price = change is not None and change > 0
    weak_price = change is not None and change <= 0
    accepted = above is True
    prior_outflow = (card.get("foreign_net_5d") or 0) < 0 or (card.get("foreign_net_20d") or 0) < 0

    # These are actual failure combinations, not generic high-price warnings.
    exhaustion = bool(
        (negative_flow and above is False and weak_price)
        or (negative_flow and weak_price and volume_ratio is not None and volume_ratio >= 1.30)
        or (above is False and change is not None and change < -1.0)
    )
    acceleration = bool(
        positive_flow and accepted and positive_price
        and (ratio is not None and ratio >= 0.10)
        and (change is not None and change >= 3.0)
        and (volume_ratio is None or volume_ratio >= 1.10)
    )
    continuation = bool(
        positive_flow and accepted and positive_price
        and ((ratio is not None and ratio >= 0.05) or (change is not None and change >= 1.5))
    )
    activation = bool(positive_flow and (accepted or positive_price))

    if exhaustion:
        trend_stage = "EXHAUSTION_FAILURE"
    elif acceleration:
        trend_stage = "ACCELERATION_ATTACK"
    elif continuation:
        trend_stage = "MAIN_UPTREND_CONTINUATION"
    elif activation:
        trend_stage = "ACTIVATING"
    elif positive_flow and not accepted:
        trend_stage = "PREPARING_TO_ACTIVATE"
    else:
        trend_stage = "NOT_STARTED"

    if negative_flow:
        capital_state = "TURNED_BEARISH"
    elif not positive_flow:
        capital_state = "SLOWING"
    elif prior_outflow:
        capital_state = "RETURNING"
    elif ((ratio is not None and ratio >= 0.10)
          or (volume_ratio is not None and volume_ratio >= 1.20)):
        capital_state = "STRENGTHENING"
    elif positive_flow:
        # A positive live A-flow is still participation evidence even when the
        # ratio/volume is not large enough to call it "strengthening".  Do not
        # downgrade a live inflow to SLOWING merely because no prior snapshot
        # was supplied.
        capital_state = "SUSTAINED"
    else:
        capital_state = "SLOWING"

    if exhaustion:
        chase_permission = "DO_NOT_CHASE"
    elif acceleration:
        chase_permission = "CHASE_BREAKOUT"
    elif continuation:
        chase_permission = "SMALL_SIZE_CHASE"
    elif activation and not accepted:
        chase_permission = "WAIT_PULLBACK"
    elif activation:
        chase_permission = "SMALL_SIZE_CHASE"
    elif positive_flow:
        chase_permission = "WAIT_PULLBACK"
    else:
        chase_permission = "DO_NOT_CHASE"

    if exhaustion:
        entry_method = "FUND_FLOW_REACCELERATION"
    elif acceleration:
        entry_method = "BREAKOUT_CHASE"
    elif not accepted:
        entry_method = "PULLBACK_ENTRY"
    elif continuation and gap is not None and gap >= 6:
        entry_method = "VWAP_SUPPORT"
    elif continuation:
        entry_method = "FUND_FLOW_REACCELERATION"
    else:
        entry_method = "VWAP_SUPPORT"

    key_broken = bool(key_price is not None and price is not None and price < key_price)
    failure_conditions = [
        {"key": "BELOW_VWAP", "active": above is False},
        {"key": "A_FLOW_TURNED_NEGATIVE", "active": negative_flow},
        {"key": "VOLUME_STALL", "active": bool(volume_ratio is not None and volume_ratio >= 1.50 and not positive_price)},
        {"key": "KEY_PRICE_BREAK", "active": key_broken},
    ]
    f5 = _num(card.get("foreign_net_5d"))
    f20 = _num(card.get("foreign_net_20d"))
    chip_basis = []
    if f5 is not None:
        chip_basis.append(f"近5日法人{'買超' if f5 > 0 else '賣超'} {f5:+,.0f} 張")
    if f20 is not None:
        chip_basis.append(f"近20日法人{'買超' if f20 > 0 else '賣超'} {f20:+,.0f} 張")
    chip_text = "、".join(chip_basis) or "法人籌碼資料不足"
    if exhaustion:
        summary = f"{chip_text}；資金與價格同步轉弱，已觸發衰竭／失敗，暫停追價。"
    elif acceleration:
        summary = f"{chip_text}；增量資金與價格接受同步加速，屬加速攻擊，可追突破，但只在突破量與 VWAP 守住時參與。"
    elif continuation and gap is not None and gap >= 5:
        summary = f"{chip_text}；目前仍在主升段且站上 VWAP，但乖離偏大、追價風險高，不取消交易資格，優先等回踩 VWAP 或資金再加速。"
    elif continuation:
        summary = f"{chip_text}；資金仍支持價格維持在 VWAP 上方，屬主升續攻，可用小部位參與，等待回踩承接或資金再加速。"
    elif activation and not accepted:
        summary = f"{chip_text}；資金已回流但價格尚未重新接受 VWAP，屬啟動中，先等回踩接或站回 VWAP，不是禁止交易。"
    elif activation:
        summary = f"{chip_text}；資金與價格開始同步，屬啟動中，可正常觀察進場位置。"
    elif positive_flow:
        summary = f"{chip_text}；資金有回流但價格尚未確認，等待價格重新接受 VWAP 或資金再度加速。"
    else:
        summary = f"{chip_text}；目前缺乏可參與的增量資金與價格確認，先不追。"
    return {
        "trend_stage": trend_stage,
        "capital_state": capital_state,
        "chase_permission": chase_permission,
        "entry_method": entry_method,
        "failure_conditions": failure_conditions,
        "participation_summary": summary,
    }


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
    price_confirmation = _price_confirmation(price, avg, change)

    lab_role = "OTHER_CONTROL"
    reversal_grade = None

    if prior_outflow and price_weak and not above_avg:
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
        reversal_state = "REVERSAL_DAY1_EARLY"
        reversal_reasons = ["A_FLOW_FLIPPED", "PRICE_REVERSED", "PERSISTENCE_NO_DATA"]
        reversal_reasons.append("ABOVE_VWAP" if price_confirmation == "CONFIRMED" else "VWAP_ACCEPTANCE_WEAKENED")
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
            action = "WATCH_PRIORITY"
            reasons = ["A_FLOW_POSITIVE", "PRIOR_OUTFLOW", "PRICE_CONFIRMATION_" + price_confirmation]
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

    card = {
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
    card.update(build_participation_judgment(card))
    return card


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


def _attach_line_b_context(cards: list[dict], line_b_payload: dict | None) -> None:
    """Attach existing C1+C2/Line-B facts without recalculating that pipeline."""
    index = {}
    for bucket in ("c1_c2_list", "intraday_discovery"):
        for row in (line_b_payload or {}).get(bucket, []) or []:
            code = str(row.get("code") or "")
            if code:
                index[code] = row

    flow_labels = {
        "OPEN_POSITIVE": "開盤即資金偏多",
        "FLOW_FLIP": "盤中資金由流出翻正",
        "NO_FLIP": "今日資金未翻正",
    }
    for card in cards:
        source = index.get(str(card.get("symbol") or ""))
        if source is None:
            card.update({
                "c1_c2_source": "NOT_IN_TODAY_LINE_B",
                "c1_c2_label": "今日未列入 C1＋C2",
                "c1_c2_note": "本日未進入原始 C1＋C2 名單，由反轉旁路觀察",
                "c1_label": "未列入今日 C1＋C2",
                "c2_label": "未列入今日 C1＋C2",
                "line_b_flow_label": None,
                "line_b_status_label": None,
                "line_b_key_price": None,
            })
            continue
        source_name = source.get("source")
        passed = source_name == "C1C2_PASS"
        card.update({
            "c1_c2_source": source_name,
            "c1_c2_label": "C1＋C2 通過" if passed else "前一日未列入 C1＋C2",
            "c1_c2_note": "原始候選：C1 結構完整、C2 賣壓減弱且價格有反應" if passed else "今日盤中反轉發現，前一日不在原始 C1＋C2 名單",
            "c1_label": "C1 結構完整" if passed and source.get("c1_structure_intact") else "前一日未列入 C1＋C2",
            "c2_label": "C2 賣壓減弱且價格有反應" if passed and source.get("c2_selling_weak_price_resp") else "前一日未列入 C1＋C2",
            "line_b_flow_class": source.get("flow_class"),
            "line_b_flow_label": flow_labels.get(source.get("flow_class"), "今日資金狀態待判讀"),
            "line_b_status_label": (source.get("explain") or {}).get("status_label"),
            "line_b_system_sentence": (source.get("explain") or {}).get("system_sentence"),
            "line_b_key_price": _num((source.get("explain") or {}).get("resistance")) or _num(source.get("t1_prior_high")),
        })


def build_live_view(
    payload: dict,
    top_n: int = 10,
    line_b_payload: dict | None = None,
    state_machine: ReversalStateMachine | None = None,
) -> dict:
    cards = [_live_card(r) for r in payload.get("rows", [])]
    _attach_line_b_context(cards, line_b_payload)
    _apply_sector_confirmation(cards)
    # Re-run after Line-B adds the optional key price used by failure checks.
    for card in cards:
        card.update(build_participation_judgment(card))
    if state_machine is not None:
        state_machine.apply(cards, payload.get("updated_at"))

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
            grade_priority.get(c.get("reversal_grade"), 9),
            role_priority.get(c["lab_role"], 8),
            -(c["change_rate"] or 0),
            -(c["aflow_ratio"] or 0),
        ),
    )
    meaningful = [c for c in reversal if c["lab_role"] != "OTHER_CONTROL"]
    reversal = (meaningful or reversal)[:20]

    state_groups = {
        "confirmed": [c for c in cards if "confirmed" in c.get("state_tags", [])],
        "failed": [c for c in cards if "failed" in c.get("state_tags", [])],
        "flip": [c for c in cards if "flip" in c.get("state_tags", [])],
        "accumulation": [c for c in cards if "accumulation" in c.get("state_tags", [])],
    }
    for group in state_groups.values():
        group.sort(key=lambda c: (-(c.get("change_rate") or 0), -(c.get("aflow_ratio") or 0)))
    pending = [c for c in cards if c.get("state_machine") == "REVERSAL_TRIGGER"]

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
        # Explicitly expose the actionable list so consumers do not confuse
        # cross-day reversal state with the current participation decision.
        "participation": reversal,
        "state_groups": state_groups,
        "state_summary": {
            "confirmed": len(state_groups["confirmed"]),
            "failed": len(state_groups["failed"]),
            "flip": len(state_groups["flip"]),
            "accumulation": len(state_groups["accumulation"]),
            "pending": len(pending),
        },
    }
