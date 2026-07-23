"""
MLS v4.0 — absorption.py
承接品質（第五模組）— 判斷賣壓被誰吸收：法人吸收(好) vs 散戶接刀(壞)。

核心洞察（反直覺）：
  融資「下降」＝散戶被洗出（好）；「增加」＝散戶接刀（壞）。

四維交叉，各 1 星，映射 1–5★：
  外資：轉買=好 / 賣超=壞
  融資：下降=好 / 增加=壞
  大戶：增加=好 / 減少=壞
  股價：不破低=好 / 破低=壞
"""


def _foreign_state(chip):
    fr = chip.get("foreign_lots", 0)
    if fr > 500:
        return "轉買"
    if fr < -500:
        return "賣超"
    return "持平"


def _margin_state(chip):
    return chip.get("margin_trend", "持平")


def _big_state(chip):
    t = chip.get("big_holder_trend")
    if t is None:
        return "持平"
    if t > 0.05:
        return "增加"
    if t < -0.05:
        return "減少"
    return "持平"


def _price_state(snap):
    # 破低：收盤 <= 昨低附近 或 跌幅深
    chg = snap.get("change_rate", 0)
    if chg <= -2:
        return "破低"
    if snap.get("close", 0) < snap.get("prev_close", 0) * 0.985:
        return "破低"
    return "不破低"


def evaluate(snap, chip):
    """回傳 (stars, detail dict)。"""
    fx = _foreign_state(chip)
    mg = _margin_state(chip)
    big = _big_state(chip)
    px = _price_state(snap)

    score = 0
    score += 1 if fx == "轉買" else 0
    score += 1 if mg == "下降" else 0          # 融資降=好（核心）
    score += 1 if big in ("增加",) else 0
    score += 1 if px == "不破低" else 0
    stars = max(1, min(5, score + 1))

    detail = {
        "stars": stars,
        "foreign": fx, "margin": mg, "big_holder": big, "price": px,
        "signals": {
            "foreign": "吸收" if fx == "轉買" else ("賣壓" if fx == "賣超" else "中性"),
            "margin": "洗散戶" if mg == "下降" else ("散戶接刀" if mg == "增加" else "中性"),
            "big_holder": "進場" if big == "增加" else ("離場" if big == "減少" else "中性"),
            "price": "止穩" if px == "不破低" else "轉弱",
        },
        "verdict": ("高承接·法人吸收" if stars >= 4 else
                    "低承接·散戶接刀" if stars <= 2 else "中性"),
    }
    return stars, detail


def is_absorption_pass(stars, min_stars):
    """漏斗第四關：承接品質是否過關。"""
    return stars >= min_stars
