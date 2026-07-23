"""MLS v4 continuous EOD health scoring engine."""
from dataclasses import dataclass
import math

PASS, FAIL, NO_DATA = "PASS", "FAIL", "NO_DATA"


@dataclass
class StockInput:
    code: str; name: str; sector: str; quadrant: str
    day_change_pct: float; active_buysell_diff: float; vol_ratio: float
    legal_20d_net: int; foreign_20d: int; trust_20d: int
    legal_5d_net: int; foreign_5d: int; trust_5d: int
    legal_consec_days: int; margin_5d_chg: int
    close: float; ma20: float; above_ma20: bool; bias_pct: float
    foreign_turn_buy: str; margin_down: str; dahu_hold: str; price_hold: str
    vs_sector_pct: float; near_limit_up: bool = False
    volume_blowout: bool = False; no_breakout: bool = False
    dahu_custody: str = NO_DATA


def _tanh(value, scale, points):
    return points * math.tanh(value / scale)


def _linear(value, lo, hi, out_lo, out_hi):
    if hi == lo:
        return out_lo
    ratio = max(0, min(1, (value - lo) / (hi - lo)))
    return out_lo + ratio * (out_hi - out_lo)


def score_legal(s):
    d = {
        "法人20日淨": round(_tanh(s.legal_20d_net, 8000, 30), 1),
        "法人5日淨": round(_tanh(s.legal_5d_net, 3000, 7), 1),
        "法人連續性": round(_linear(s.legal_consec_days, -3, 3, -3, 3), 1),
    }
    return sum(d.values()), d


def score_absorption(s):
    mapping = {
        "外資轉買": (s.foreign_turn_buy, 6, -6),
        "融資下降": (s.margin_down, 5, -3),
        "大戶": (s.dahu_hold, 5, -5),
        "股價不破低": (s.price_hold, 4, -6),
    }
    d = {}
    for key, (state, good, bad) in mapping.items():
        d[key] = good if state == PASS else bad if state == FAIL else 0
    return sum(d.values()), d


def score_technical(s):
    total = 10 if s.above_ma20 else -10
    b = s.bias_pct
    if -5 <= b <= 10:
        bias = _linear(abs(b - 2.5), 0, 7.5, 6, 2)
    elif b > 10:
        bias = _linear(b, 10, 25, 2, -6)
    else:
        bias = _linear(b, -5, -20, 0, -8)
    active = _tanh(s.active_buysell_diff, 0.5, 4)
    d = {"站上MA20": 10 if s.above_ma20 else -10,
         "乖離": round(bias, 1), "主動買賣差": round(active, 1)}
    return total + bias + active, d


def score_flow(s):
    quadrant = 6 if "流入" in s.quadrant else -6 if "流出" in s.quadrant else 0
    day = _tanh(s.day_change_pct, 5, 6)
    return quadrant + day, {"象限": quadrant, "今日動能": round(day, 1)}


def score_sector(s):
    points = _tanh(s.vs_sector_pct, 4, 8)
    return points, {"vs族群": round(points, 1)}


def apply_risk(score, s):
    hard, soft = [], []
    if s.margin_5d_chg > 2000 and (s.day_change_pct > 7 or s.near_limit_up):
        hard.append("噴漲+融資暴增（散戶追高）")
    if not s.above_ma20 and s.legal_20d_net < 0:
        hard.append("跌破月線+法人賣超（結構破壞）")
    if s.near_limit_up or s.volume_blowout:
        soft.append("接近漲停/爆量")
    if s.no_breakout:
        soft.append("未突破前高")
    if s.dahu_custody == NO_DATA:
        soft.append("大戶集保無資料（不計分）")
    score -= 2 * len(soft)
    if hard:
        score = min(score, 60)
    return score, hard, soft


def compute_health_score(s):
    legal, legal_d = score_legal(s)
    absorption, absorption_d = score_absorption(s)
    technical, technical_d = score_technical(s)
    flow, flow_d = score_flow(s)
    sector, sector_d = score_sector(s)
    raw = legal + absorption + technical + flow + sector
    base = max(0, min(100, (raw + 100) / 2))
    final, hard, soft = apply_risk(base, s)
    final = max(0, min(100, final))
    grade = "Watch" if hard else ("Ready" if final >= 80 else "Ready候選" if final >= 68 else "Watch")
    stars = 1 + round(_linear(absorption, -20, 20, 0, 4))
    return {
        "code": s.code, "name": s.name, "score": round(final, 1),
        "base_before_risk": round(base, 1), "grade": grade, "stars": stars,
        "dims": {"法人動能(40)": round(legal, 1), "承接品質(20)": round(absorption, 1),
                 "技術面(20)": round(technical, 1), "資金流向(12)": round(flow, 1),
                 "族群(8)": round(sector, 1)},
        "detail": {"法人": legal_d, "承接": absorption_d, "技術": technical_d,
                   "流向": flow_d, "族群": sector_d},
        "hard_risk": hard, "soft_risk": soft, "sort_weight": sector,
    }
