"""
MLS 標準版 — scoring.py
盤中決策評分引擎 v2:五因子加權 + 懲罰項 + 環境係數 + 權重自學習。
全部公式依使用者交接檔鐵律設計:
  鐵律2/8(資金欄假紅=出貨) → 個股層級價量背離懲罰
  死加總陷阱             → 量能用時段正規化 TNVR,不用原始累計
  引擎/攻擊部隊           → 由 engine.py 前置處理,本模組只算分
"""

from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))

# ── 台股全日累積成交量佔比曲線(U型;分鐘線性內插) ──────
#   (分鐘自09:00起, 累積佔比)
VOLUME_CURVE = [
    (0, 0.04), (15, 0.14), (30, 0.22), (60, 0.34), (90, 0.44),
    (120, 0.52), (150, 0.60), (180, 0.67), (210, 0.75),
    (240, 0.85), (270, 1.00),
]


def expected_volume_frac(now=None):
    """現在時刻的全日量預期累積佔比 f(t)。盤外回傳 1.0。"""
    now = now or datetime.now(TW_TZ)
    m = (now.hour - 9) * 60 + now.minute
    if m <= 0:
        return VOLUME_CURVE[0][1]
    if m >= 270:
        return 1.0
    for (m0, f0), (m1, f1) in zip(VOLUME_CURVE, VOLUME_CURVE[1:]):
        if m0 <= m <= m1:
            return f0 + (f1 - f0) * (m - m0) / (m1 - m0)
    return 1.0


def tnvr(total_volume, avg5_volume, now=None):
    """時段正規化量比。avg5_volume 缺值時退回 None(不給量能分)。"""
    if not avg5_volume:
        return None
    frac = max(0.04, expected_volume_frac(now))
    return round(total_volume / (avg5_volume * frac), 2)


# ── 盤中主動買賣淨流(跨輪累積) ─────────────────────────
_prev_vol = {}      # code → 上一輪累積量
_aflow = {}         # code → 主動淨流(股;+主動買 −主動賣)


def update_aflow(code, total_volume, tick_type):
    """
    每輪掃描呼叫:把兩輪之間的量增量,依當前 tick_type 記為主動買/賣。
    tick_type: Shioaji 1=賣盤成交(內盤) 2=買盤成交(外盤);FinMind 同義。
    近似法——盤中無逐筆時的最佳估計。
    """
    prev = _prev_vol.get(code, total_volume)
    delta = max(0, (total_volume or 0) - prev)
    _prev_vol[code] = total_volume or 0
    sign = 0
    t = str(tick_type)
    if t in ("2", "TickType.Buy"):
        sign = 1
    elif t in ("1", "TickType.Sell"):
        sign = -1
    _aflow[code] = _aflow.get(code, 0) + delta * sign
    return _aflow[code]


def reset_aflow():
    """每日開盤前清空。"""
    _prev_vol.clear()
    _aflow.clear()


def get_aflow(code):
    return _aflow.get(code, 0)


# ── 價量背離偵測(鐵律2/8 → 個股層級) ─────────────────
def divergence(change_rate, aflow, total_volume):
    """
    回傳 ('fake_red'|'pull_sell'|None, 說明)
    fake_red : 價跌但主動淨流為正 → 賣壓砸出的量被記主動買(假紅)
    pull_sell: 價漲但主動淨流為負 → 邊拉邊賣(拉高出貨)
    淨流須達當日量 8% 才算顯著,避免雜訊。
    """
    if not total_volume:
        return None, ""
    ratio = aflow / total_volume
    if change_rate <= -1.0 and ratio > 0.08:
        return "fake_red", "假紅背離:價跌但主動買淨流為正,出貨疑慮,等外資蓋章"
    if change_rate >= 1.0 and ratio < -0.08:
        return "pull_sell", "邊拉邊賣:價漲但主動賣淨流為負,防漲完隔天倒"
    return None, ""


# ── 規則權重(自學習;由 db rule_stats 更新,開盤載入) ──
DEFAULT_WEIGHTS = {
    "trend": 1.0, "volume": 1.0, "rs": 1.0, "chip": 1.0, "sector": 1.0,
}
_weights = dict(DEFAULT_WEIGHTS)


def load_weights(w):
    """w: dict,缺鍵用預設。clamp 0.6~1.5。"""
    for k in DEFAULT_WEIGHTS:
        v = w.get(k)
        if v is not None:
            _weights[k] = min(1.5, max(0.6, float(v)))


# ── 五因子總評分 ───────────────────────────────────────
MODE_MULT = {"attack": 1.0, "caution": 0.85, "risk": 0.6}


def score_stock(s, *, sector_median, market_pct, locked, abab_a_day,
                chip, tnvr_val, aflow_val, ma_bias=None, mode="attack"):
    """
    s: snapshot dict(price/change_rate/high/avg_price/total_volume/open)
    回傳 (score:int 1-99, factors:dict, penalties:list[str], div_flag)
    """
    F = {"trend": 0, "volume": 0, "rs": 0, "chip": 0, "sector": 0}
    pen, pen_pts = [], 0

    price, chg = s["price"] or 0, s["change_rate"] or 0
    avgp = s.get("avg_price") or 0

    # 趨勢 25
    if avgp and price >= avgp and chg > 0:
        F["trend"] += 10
    if s.get("high") and price >= s["high"] and chg > 0:
        F["trend"] += 10
    if s.get("prev_high") and price > s["prev_high"]:
        F["trend"] += 5

    # 量能 25(TNVR 分段)
    if tnvr_val is not None:
        if tnvr_val >= 2.5:   F["volume"] = 25
        elif tnvr_val >= 1.8: F["volume"] = 18
        elif tnvr_val >= 1.3: F["volume"] = 10

    # 相對強度 20
    rs_sec = chg - sector_median
    rs_mkt = chg - market_pct
    if rs_sec > 1.0:  F["rs"] += 12
    elif rs_sec > 0:  F["rs"] += 6
    if rs_mkt > 0:    F["rs"] += 8

    # 籌碼 20(盤後快取)
    if chip:
        if (chip.get("inst_net_20d_lots") or 0) > 0: F["chip"] += 10
        if (chip.get("inst_streak") or 0) >= 3:      F["chip"] += 5
        if (chip.get("big_holder_trend") or 0) > 0:  F["chip"] += 5

    # 族群 10
    if locked:      F["sector"] += 8
    if abab_a_day:  F["sector"] += 2

    # ── 懲罰項 ──
    div_flag, div_msg = divergence(chg, aflow_val, s.get("total_volume"))
    if div_flag == "fake_red":
        pen.append(div_msg); pen_pts += 15
    elif div_flag == "pull_sell":
        pen.append(div_msg); pen_pts += 12
    if ma_bias is not None and ma_bias > 8:
        pen.append(f"乖離{ma_bias:.1f}%過熱"); pen_pts += 10
    if tnvr_val is not None and tnvr_val > 2 and 0 <= chg < 1:
        pen.append("爆量滯漲"); pen_pts += 8

    raw = sum(F[k] * _weights[k] for k in F) - pen_pts
    score = int(max(1, min(99, raw * MODE_MULT.get(mode, 1.0))))
    return score, F, pen, div_flag
