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


# ─────────────────────────────────────────────────────────────
# Tick 軌跡追蹤器(第四條件根基:deque + BS ratio + 5 分鐘近端)
# 對應文件二:「累積比 + 近5分比」即時觸發
from collections import deque
import time as _time

class TickTracker:
    """
    對單檔股票:追蹤每筆 tick,提供:
      - add_tick(price, volume, ts=None): 加一筆(price, volume)
        · 若外部已給 buy/sell 歸類 → 用外部歸類
        · 否則 fallback 用價差歸類(>前價=買,<前價=賣)
      - 5min_buy / 5min_sell:近 300 秒內買賣量
      - cum_buy / cum_sell:開盤以來總買賣量
      - cum_ratio / recent_5min_ratio
    用 deque 防記憶體肥大,每次 add_tick 順手 popleft 過期資料。
    """
    def __init__(self, window_sec=300):
        self.tick_history = deque()             # [(ts, price, volume, side)]  side:+1=buy / -1=sell / 0=中立
        self.total_buy_vol = 0
        self.total_sell_vol = 0
        self.last_price = None
        self.window_sec = window_sec

    def add_tick(self, price, volume, ts=None, side=None):
        if ts is None:
            ts = _time.time()
        # 過期清理
        while self.tick_history and (ts - self.tick_history[0][0] > self.window_sec):
            old = self.tick_history.popleft()
            # 從總量扣掉已滑出視窗的買賣量(避免遺留)
            if old[3] == 1 and self.total_buy_vol >= old[2]:
                self.total_buy_vol -= old[2]
            elif old[3] == -1 and self.total_sell_vol >= old[2]:
                self.total_sell_vol -= old[2]
        # 決定方向
        if side is None:
            if self.last_price is None:
                side = 0
            elif price > self.last_price:
                side = 1
            elif price < self.last_price:
                side = -1
            else:
                side = 0
        self.last_price = price
        self.tick_history.append((ts, price, volume, side))
        if side == 1:
            self.total_buy_vol += volume
        elif side == -1:
            self.total_sell_vol += volume

    @property
    def cum_ratio(self):
        """累積買賣比(文件二:主 BS 比);若 sell=0 視為 inf,改回傳大值。"""
        return (self.total_buy_vol / self.total_sell_vol) if self.total_sell_vol > 0 else float('inf')

    def recent_5min_buy(self):
        return sum(v for (t, _, v, side) in self.tick_history if side == 1)

    def recent_5min_sell(self):
        return sum(v for (t, _, v, side) in self.tick_history if side == -1)

    @property
    def recent_5min_ratio(self):
        s = self.recent_5min_sell()
        return (self.recent_5min_buy() / s) if s > 0 else float('inf')

    def reset(self):
        self.tick_history.clear()
        self.total_buy_vol = 0
        self.total_sell_vol = 0
        self.last_price = None


# 全域 tracker dict(每個 code 一個 TickTracker)
_trackers = {}

def get_tracker(code):
    """取得(或建立)指定股票的 TickTracker。跨盤中持續累積,隔日由 reset_all_trackers() 清空。"""
    if code not in _trackers:
        _trackers[code] = TickTracker()
    return _trackers[code]

def reset_all_trackers():
    """開盤日切換時呼叫(08:30 / 跨日)。"""
    _trackers.clear()


# ── BS Ratio 主動買賣盤濾網(第四道關卡) ─────────────────
# 資料源:Shioaji 快照 buy_volume(外盤)/sell_volume(內盤),全場累積。
# 誠實邊界:「近5分鐘 BS 比」需逐筆 tick,快照給不了;
#   這裡全場累積 BS 用真數據,近端動能改用量比加速度近似(標注 approx)。
_bs_prev = {}     # code -> (prev_buy, prev_sell) 上一輪,估算近端動能


def bs_ratio(buy_vol, sell_vol):
    """全場累積買賣比 %:Buy/(Buy+Sell)*100。無資料回 None。"""
    tot = (buy_vol or 0) + (sell_vol or 0)
    if tot <= 0:
        return None
    return round((buy_vol or 0) / tot * 100, 1)


def bs_recent(code, buy_vol, sell_vol):
    """近端主動買賣比(用兩輪快照增量近似『近幾分鐘』;非逐筆,標 approx)。"""
    pb, ps = _bs_prev.get(code, (buy_vol, sell_vol))
    _bs_prev[code] = (buy_vol or 0, sell_vol or 0)
    db = max(0, (buy_vol or 0) - pb)
    ds = max(0, (sell_vol or 0) - ps)
    tot = db + ds
    if tot <= 0:
        return None
    return round(db / tot * 100, 1)


def reset_bs():
    _bs_prev.clear()


def dynamic_bs_threshold(market_pct):
    """市場風向動態倍數(對應規格書):強多1.1 / 溫和1.25 / 下跌1.5。"""
    if market_pct > 1:   return 1.10
    if market_pct >= 0:  return 1.25
    return 1.50


def bs_filter(buy_vol, sell_vol, market_pct, intraday=True, recent_pct=None):
    """
    第四道濾網。回傳 (pass:bool, detail:dict)
    盤中:全場累積 BS 比對應 >1.2倍門檻 且 近端 approx >1.5倍(有資料才要求)
    盤後:全場累積 BS 比對應 >1.25倍(動態倍數)
    """
    br = bs_ratio(buy_vol, sell_vol)
    if br is None:
        return False, {"bs": None, "note": "無內外盤資料"}
    # 倍數換算成 BS%:ratio=倍數 → BS% = 倍數/(倍數+1)*100
    if intraday:
        mult = 1.2
        need = mult / (mult + 1) * 100         # ≈54.5%
        ok = br > need
        if recent_pct is not None:             # 有近端資料才加嚴
            need5 = 1.5 / 2.5 * 100            # =60%
            ok = ok and recent_pct > need5
        return ok, {"bs": br, "recent_bs_approx": recent_pct,
                    "need": round(need, 1), "mult": mult}
    else:
        mult = dynamic_bs_threshold(market_pct)
        need = mult / (mult + 1) * 100
        return br > need, {"bs": br, "need": round(need, 1), "mult": mult}


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


# ─────────────────────────────────────────────────────────────
# 即時觸發條件(文件二:四條件進場判斷)
# · cond1:大盤現價 > 大盤開盤價
# · cond2:股價現價 > 今日前 30 分最高價
# · cond3:預估量 > 昨日量 × 1.2
# · cond4(本檔新增):累積比 + 近5分比,寬鬆/嚴苛兩模式(開盤30分鐘切換)
#
# 觸發時機:每秒或每分鐘被 add_tick 餵入後呼叫 evaluate_realtime(...)。
# 回傳 dict:{cond1..cond4: bool, fired: bool, reasons: [str], mode: 'loose'|'strict'}

from datetime import datetime, timedelta
try:
    _TW_TZ = timezone(timedelta(hours=8))
except Exception:
    _TW_TZ = None

def _elapsed_min_from_open(now=None):
    """距離 09:00 開盤已過幾分鐘。"""
    if now is None:
        now = datetime.now(_TW_TZ) if _TW_TZ else datetime.now()
    open_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
    delta = (now - open_dt).total_seconds() / 60
    return max(0, int(delta))

def condition4_realtime(tracker, *, loose_first_30min=True):
    """
    第四條件:雙重 BS 保險(累積比 + 近 5 分比)
    開盤 30 分鐘內寬鬆,之後嚴苛。
      寬鬆:累積 > 1.1 且 近5分 > 1.2
      嚴苛:累積 > 1.2 且 近5分 > 1.5
    """
    elapsed = _elapsed_min_from_open()
    cum = tracker.cum_ratio
    rec = tracker.recent_5min_ratio
    if loose_first_30min and elapsed <= 30:
        return (cum > 1.1) and (rec > 1.2), elapsed, "loose"
    return (cum > 1.2) and (rec > 1.5), elapsed, "strict"


def evaluate_realtime(*, code, current_price, market_open_price, market_current_price,
                      day_30m_high, est_volume, prev_day_volume, tracker=None,
                      loose_first_30min=True):
    """
    文件二的四條件總判斷。
    cond1 大盤現價 > 大盤開盤價
    cond2 個股現價 > 今日前 30 分鐘最高價
    cond3 預估量 > 昨日量 × 1.2
    cond4 TickTracker 累積比 + 近5分比(寬鬆/嚴苛模式)
    """
    cond1 = (market_current_price or 0) > (market_open_price or 0)
    cond2 = (current_price or 0) > (day_30m_high or 0)
    cond3 = (est_volume or 0) > (prev_day_volume or 0) * 1.2
    reasons = []
    if not cond1: reasons.append("cond1 大盤未過開盤價")
    if not cond2: reasons.append("cond2 未破前30分高")
    if not cond3: reasons.append("cond3 預估量不足")

    cond4_passed = False
    elapsed = 0
    mode = "strict"
    if tracker is not None:
        cond4_passed, elapsed, mode = condition4_realtime(tracker,
                                                          loose_first_30min=loose_first_30min)
        if not cond4_passed:
            cum = tracker.cum_ratio
            rec = tracker.recent_5min_ratio
            rsn = []
            try:
                if cum != float('inf'): rsn.append(f"累積{cum:.2f}")
            except Exception: pass
            try:
                if rec != float('inf'): rsn.append(f"近5分{rec:.2f}")
            except Exception: pass
            reasons.append(f"cond4 {'寬鬆' if mode=='loose' else '嚴苛'}未過 ({','.join(rsn) or '資料不足'}, 開盤第{elapsed}分)")
    else:
        reasons.append("cond4 無 TickTracker")

    fired = cond1 and cond2 and cond3 and cond4_passed
    return {
        "code": code,
        "fired": fired,
        "cond1": cond1,
        "cond2": cond2,
        "cond3": cond3,
        "cond4": cond4_passed,
        "elapsed_min": elapsed,
        "mode": mode,
        "reasons": reasons,
        "cum_ratio": (tracker.cum_ratio if tracker else None),
        "recent_ratio": (tracker.recent_5min_ratio if tracker else None),
    }
