"""Pure chip-price divergence classifier.

Rows are newest first.  The scanner emits one mutually exclusive diagnostic
and never authorizes rejection; structural failure remains the only drop gate.
"""

from __future__ import annotations

from statistics import median


TYPE_META = {
    "chip_reversal": ("🔥 籌碼反轉", "S", "highest", "upgrade_a"),
    "sell_absorption": ("🟢 抗賣壓", "A", "high", "prioritize"),
    "washout": ("🟢 洗盤換手", "B", "high", "pullback_watch"),
    "buying_stall": ("🟡 買盤鈍化", "D", "low", "no_chase"),
    "double_weak": ("🔴 籌碼價格雙殺", "E", "low", "downgrade"),
    "none": ("", "none", "normal", "none"),
}


def _num(value):
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _values(rows, key, n):
    return [_num(row.get(key)) for row in rows[:n]]


def _sum(values):
    return sum(value for value in values if value is not None)


def _change(new, old):
    return ((new - old) / old * 100.0) if new is not None and old else None


def _signed_streak(inst_rows):
    supplied = _num((inst_rows[0] if inst_rows else {}).get("consecutive_days"))
    if supplied is not None:
        return int(supplied)
    values = _values(inst_rows, "total_net", len(inst_rows))
    if not values or values[0] in (None, 0):
        return 0
    sign = 1 if values[0] > 0 else -1
    count = 0
    for value in values:
        if value is None or value == 0 or (1 if value > 0 else -1) != sign:
            break
        count += 1
    return sign * count


def _low_trend_not_falling(lows):
    if len(lows) < 3 or any(value is None for value in lows[:3]):
        return False
    # Newest first: falling lows would be newest < previous < oldest.
    return not (lows[0] < lows[1] < lows[2])


def _base(pending, metrics):
    return {
        "divergence_type": "none", "divergence_label": "",
        "divergence_grade": "none", "divergence_priority": "normal",
        "divergence_action": "none", "divergence_reasons": [],
        "divergence_metrics": metrics, "divergence_pending": sorted(set(pending)),
        "matched_types": [], "can_reject": False,
    }


def scan(inst_rows: list[dict] | None, bar_rows: list[dict] | None,
         aflow_rows: list[dict] | None) -> dict:
    inst_rows, bar_rows, aflow_rows = list(inst_rows or []), list(bar_rows or []), list(aflow_rows or [])
    pending = []
    if len(inst_rows) < 5:
        pending.append("近5日法人")
    if len(bar_rows) < 5:
        pending.append("近5日價格")
    pending.append("法人持股比例")

    inst5 = _values(inst_rows, "total_net", 5)
    inst20 = _values(inst_rows, "total_net", 20)
    closes = _values(bar_rows, "close", 20)
    highs = _values(bar_rows, "high", 20)
    lows = _values(bar_rows, "low", 20)
    volumes = _values(bar_rows, "volume", 5)
    today = bar_rows[0] if bar_rows else {}
    close = _num(today.get("close")); high = _num(today.get("high")); low = _num(today.get("low"))
    ma20 = _num(today.get("ma20")); vma = _num(today.get("vol_ma20")); volume = _num(today.get("volume"))
    change = _change(close, closes[1]) if len(closes) > 1 else None
    price5 = _change(close, closes[4]) if len(closes) >= 5 else None
    streak = _signed_streak(inst_rows)
    inst3_sum = _sum(inst5[:3]); inst5_sum = _sum(inst5)
    inst20_sum = _sum(inst20)
    volume5 = _sum(volumes)
    inst_share5 = (inst5_sum / volume5 * 100.0) if volume5 and len(inst5) >= 5 else None
    vol_ratio = (volume / vma) if volume is not None and vma else None
    close_position = ((close - low) / (high - low)) if None not in (close, high, low) and high > low else None
    efficiency = ((max(0.0, price5) / inst_share5) if price5 is not None and
                  inst_share5 is not None and inst_share5 > 0 else None)
    metrics = {
        "institution_streak": streak, "institution_3d": inst3_sum,
        "institution_5d": inst5_sum, "institution_20d": inst20_sum,
        "price_return_5d": round(price5, 2) if price5 is not None else None,
        "institution_share_5d": round(inst_share5, 2) if inst_share5 is not None else None,
        "buying_efficiency_proxy": round(efficiency, 2) if efficiency is not None else None,
        "volume_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "close_position": round(close_position, 2) if close_position is not None else None,
    }
    result = _base(pending, metrics)
    if len(inst_rows) < 5 or len(bar_rows) < 5 or any(value is None for value in inst5 + closes[:5]):
        return result

    today_inst = inst5[0]
    prior5 = _values(inst_rows[1:], "total_net", 5)
    prior_negative_days = sum(value is not None and value < 0 for value in prior5)
    prior_negative_sum = _sum(prior5)
    prior_abs = [abs(value) for value in prior5 if value is not None]
    prior_high = highs[1] if len(highs) > 1 else None
    breakout = close is not None and prior_high is not None and close > prior_high
    c_hit = (len(prior5) == 5 and prior_negative_sum < 0 and prior_negative_days >= 3 and
             today_inst is not None and today_inst > 0 and prior_abs and
             today_inst >= median(prior_abs) * 1.5 and
             (breakout or (change is not None and change >= 3.0)) and
             vol_ratio is not None and vol_ratio >= 1.2 and
             close_position is not None and close_position >= 0.7)

    previous_lows = [value for value in lows[1:6] if value is not None]
    low_holds = low is not None and previous_lows and low >= min(previous_lows)
    a_hit = (streak <= -2 and inst5_sum < 0 and change is not None and change >= -1.5 and
             low_holds and _low_trend_not_falling(lows))

    positive_average = (_sum([value for value in inst5[1:] if value and value > 0]) /
                        max(1, sum(value is not None and value > 0 for value in inst5[1:])))
    no_large_sell = today_inst is not None and today_inst >= -positive_average * 0.5
    b_hit = (streak >= 3 and inst5_sum > 0 and change is not None and -5.0 <= change <= -1.0 and
             no_large_sell and close is not None and ma20 is not None and close >= ma20 and low_holds)

    near_high20 = (close is not None and any(value is not None for value in highs) and
                   close >= max(value for value in highs if value is not None) * 0.97)
    failed_new_high = high is not None and any(value is not None for value in highs[1:]) and high <= max(
        value for value in highs[1:] if value is not None)
    high_risk = near_high20 and vol_ratio is not None and vol_ratio >= 1.5 and failed_new_high
    d_hit = (streak >= 3 and inst5_sum > 0 and inst_share5 is not None and inst_share5 >= 2.0 and
             price5 is not None and (price5 <= 1.0 or (change is not None and change < 0)))

    prior_closes = [value for value in closes[1:6] if value is not None]
    e_hit = (streak <= -2 and inst5_sum < 0 and close is not None and prior_closes and
             close < min(prior_closes) and change is not None and change <= -1.5)

    if c_hit:
        type_name = "chip_reversal"
        reasons = ["前5日法人流出", "今日法人轉買", "突破放量"]
    elif a_hit:
        type_name = "sell_absorption"
        reasons = [f"法人連賣{abs(streak)}日", "5日淨流出", "低點未破"]
    elif b_hit:
        type_name = "washout"
        reasons = [f"法人連買{streak}日", "單日回檔", "趨勢未破"]
    elif d_hit:
        type_name = "buying_stall"
        reasons = ["5日法人買超", "價格未動", "高檔風險" if high_risk else "買盤效率低"]
    elif e_hit:
        type_name = "double_weak"
        reasons = [f"法人連賣{abs(streak)}日", "跌破5日低", "籌碼價格同弱"]
    else:
        return result

    label, grade, priority, action = TYPE_META[type_name]
    result.update({
        "divergence_type": type_name, "divergence_label": label,
        "divergence_grade": grade, "divergence_priority": priority,
        "divergence_action": action, "divergence_reasons": reasons[:3],
        "matched_types": [type_name], "can_reject": False,
    })
    return result
