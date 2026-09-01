"""
MLS 模組 — money_health_api.py（資金健康度 · 證據卡後端 v3 純盤後）
====================================================================
僅供盤後分析使用，所有數據來自已落地資料與官方日K：
  - eod_snapshot：收盤價、漲跌幅、量比、族群、aflow_ratio（若有）
  - daily_bars 或 TWSE/TPEx 官方月K：歷史 K 線（計算均線、ATR、前高）

無任何盤中即時資料來源，也不依賴 Shioaji 金鑰。
若資料庫缺 K 線，技術分自動降級但健康分仍由其他模組支撐。
====================================================================
"""
from __future__ import annotations
import sqlite3
import json
import math
import os
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

# ── 選配依賴（僅 config 用於名稱／族群映射） ──────────────
try:
    import config as C
except ImportError:
    C = None

try:
    import chip_provider
except ImportError:
    chip_provider = None

TW_TZ = timezone(timedelta(hours=8))
DB_PATH = Path(os.environ.get("MLS_DB_PATH", str(Path(__file__).with_name("mls.db"))))

# ═══════════════════════════════════════════════════════════
# 參數集中區（調權重／門檻就在這裡）
# ═══════════════════════════════════════════════════════════
class PARAMS:
    # 四模組權重（總和=1.0，chip 缺資料時自動重分配）
    W_TECH = 0.35
    W_CAP = 0.30
    W_CHIP = 0.20
    W_SECTOR = 0.15

    # 技術面連續打分閾值
    MA20_LEN = 20
    MA60_LEN = 60
    MA20_BIAS_MAX = 5.0      # 偏離 5% 得滿分 25
    MA60_BIAS_MAX = 10.0     # 偏離 10% 得滿分 15
    BREAKOUT_DIST_MAX = 5.0  # 距前高 5% 內線性給分
    BREAKOUT_LOOKBACK = 10   # 突破確認只比較前 10 個交易日高點
    BIAS_OVER = 8.0          # 過度乖離觸發軟風險

    # 資金流門檻
    FLOW_EPS = 0.02

    # 風險門檻
    NEAR_LIMIT_CLOSE_PCT = 0.98  # 收盤達漲停價 98% 才標示「接近漲停」
    VOL_SPIKE = 2.5
    RESIST_NEAR = 1.5

    # 分級門檻
    READY_MIN = 75
    WATCH_MIN = 60

    # 交易計畫
    ATR_LEN = 14
    ATR_STOP_MULT = 1.5
    ATR_TGT_MULT = 3.0

    # AI 勝率基底
    AI_BASE_D1, AI_BASE_D5 = 30, 30


# ═══════════════════════════════════════════════════════════
# 資料庫讀取函數（唯讀，無任何外部呼叫）
# ═══════════════════════════════════════════════════════════
def _yahoo_daily_bars(code: str, days: int, asof: Optional[str]) -> List[Dict]:
    """官方／FinMind歷史不足時的完整日K備援；僅供技術指標，不混充官方收盤。"""
    end = datetime.strptime(asof, "%Y-%m-%d").replace(tzinfo=TW_TZ) if asof else datetime.now(TW_TZ)
    start = end - timedelta(days=max(days * 3, 180))
    query = urllib.parse.urlencode({
        "period1": int(start.timestamp()), "period2": int((end + timedelta(days=1)).timestamp()),
        "interval": "1d",
    })
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW?{query}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MLS NEXORA"})
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))["chart"]["result"][0]
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        out = []
        for ts, close, high, low, volume in zip(result.get("timestamp") or [], quote.get("close") or [],
                                                quote.get("high") or [], quote.get("low") or [],
                                                quote.get("volume") or []):
            if close is None or high is None or low is None:
                continue
            date = datetime.fromtimestamp(ts, TW_TZ).strftime("%Y-%m-%d")
            if asof and date > asof:
                continue
            out.append({"date": date, "open": close, "high": float(high), "low": float(low),
                        "close": float(close), "volume": float(volume or 0), "source": "yahoo_history"})
        return out[-days:]
    except Exception as e:
        print(f"[money_health_api] Yahoo 日K讀取失敗 {code}: {e}")
        return []


def _read_daily_bars(code: str, days: int = 70, asof: Optional[str] = None) -> List[Dict]:
    """
    從 daily_bars 表讀取 K 線（最新 days 根），若表不存在或無資料回傳空串列。
    回傳格式：[{date, open, high, low, close, volume}, ...]（時間升序）
    """
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=2)
            cur = conn.execute(
                "SELECT date, open, high, low, close, volume FROM daily_bars WHERE code=? ORDER BY date DESC LIMIT ?",
                (code, days)
            )
            rows = cur.fetchall()
            conn.close()
            if rows:
                bars = []
                for r in reversed(rows):
                    bars.append({
                        "date": r[0],
                        "open": float(r[1]) if r[1] else 0.0,
                        "high": float(r[2]) if r[2] else 0.0,
                        "low": float(r[3]) if r[3] else 0.0,
                        "close": float(r[4]) if r[4] else 0.0,
                        "volume": float(r[5]) if r[5] else 0.0,
                    })
                return [b for b in bars if not asof or str(b["date"])[:10] <= asof]
        except Exception as e:
            print(f"[money_health_api] DB 日K讀取失敗 {code}: {e}")

    # DB 沒有日K時改讀 eod_source：優先 TWSE/TPEx 官方，受阻時退 FinMind 日K。
    # 不可改呼叫 Shioaji：VPS 盤後服務未持有交易 API 金鑰，失敗會靜默退成空資料。
    try:
        import eod_source
        end = datetime.strptime(asof, "%Y-%m-%d") if asof else datetime.now(TW_TZ)
        start = (end - timedelta(days=max(days * 3, 180))).strftime("%Y-%m-%d")
        rows = eod_source._price_rows(code, start, trade_date=end.strftime("%Y-%m-%d"))
        by_date: Dict[str, Dict] = {}
        for row in rows:
            date = str(row.get("date") or "")[:10]
            if not date or (asof and date > asof):
                continue
            close = row.get("close")
            high = row.get("max")
            low = row.get("min")
            if close is None or high is None or low is None:
                continue
            by_date[date] = {
                "date": date,
                "open": row.get("open") or close,
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(row.get("Trading_Volume") or 0),
            }
        official_bars = [by_date[date] for date in sorted(by_date)[-days:]]
        if len(official_bars) >= min(20, days):
            return official_bars
        yahoo_bars = _yahoo_daily_bars(code, days, asof)
        return yahoo_bars or official_bars
    except Exception as e:
        print(f"[money_health_api] 官方日K讀取失敗 {code}: {e}")
        return []


def _read_eod_snapshots(trade_date: str = None) -> List[Dict]:
    """
    從 eod_snapshot 讀取收盤快照。
    - 若 trade_date 指定，讀取該日；否則讀取最新一日。
    - 回傳 list of dict，每個 dict 含 code, price, change_rate, volume_ratio, sector, aflow_ratio 等。
    """
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=2)
        if trade_date:
            cur = conn.execute(
                "SELECT payload FROM eod_snapshot WHERE trade_date=?",
                (trade_date,)
            )
        else:
            cur = conn.execute(
                "SELECT payload FROM eod_snapshot WHERE trade_date=(SELECT MAX(trade_date) FROM eod_snapshot)"
            )
        rows = cur.fetchall()
        conn.close()
        snaps = []
        for r in rows:
            try:
                snaps.append(json.loads(r[0]))
            except:
                continue
        return snaps
    except Exception as e:
        print(f"[money_health_api] 讀取 eod_snapshot 錯誤: {e}")
        return []


# ═══════════════════════════════════════════════════════════
# 小工具
# ═══════════════════════════════════════════════════════════
def _clip(x, lo=0, hi=100):
    return int(max(lo, min(hi, round(x))))


def _today():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


def _sma(vals, n):
    v = [x for x in vals if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if len(v) < n:
        return None
    return sum(v[-n:]) / n


def _atr(bars, n=PARAMS.ATR_LEN):
    if len(bars) < 2:
        return None
    trs = []
    for i in range(1, len(bars)):
        h = bars[i].get("high")
        l = bars[i].get("low")
        pc = bars[i - 1].get("close")
        if None in (h, l, pc):
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    return sum(trs[-n:]) / min(len(trs), n)


# ═══════════════════════════════════════════════════════════
# 一、四模組計分（公式集中，回傳分數＋證據＋資料品質）
# ═══════════════════════════════════════════════════════════
def score_technical(snap: Dict, bars: List[Dict]) -> tuple:
    """
    技術分（連續偏離 % 線性加權）：
      - 站上 MA20：偏離 0~5% → 0~25 分；跌破則 -20~0
      - 站上 MA60：偏離 0~10% → 0~15 分；跌破則 -12~0
      - 量價同向：漲時量比 0.8~1.5 → 0~20；跌時量縮扣 0~5
      - 突破前高：距前高 -5%~0 → 0~20；已突破 +0~1% → 20~25
      - 過度乖離 |bias| ≥ 8% 扣 10 分
    起點 0，理論 0~100，clip 處理。
    回傳 (score, evidence, quality)
    """
    price = snap.get("price") or snap.get("close")
    chg = snap.get("change_rate") or 0
    vr = snap.get("volume_ratio") or 1.0
    closes = [b.get("close") for b in bars if b.get("close") is not None]
    ma20 = _sma(closes, PARAMS.MA20_LEN)
    ma60 = _sma(closes, PARAMS.MA60_LEN)

    quality = "ok"
    if price is None:
        quality = "missing"
    elif ma20 is None and ma60 is None:
        quality = "degraded"

    # A. 站上 MA20（連續）
    if price and ma20:
        bias20 = (price - ma20) / ma20 * 100
        if bias20 >= 0:
            s_ma20 = min(25, 25 * min(bias20, PARAMS.MA20_BIAS_MAX) / PARAMS.MA20_BIAS_MAX)
        else:
            s_ma20 = max(-20, 20 * max(bias20, -PARAMS.MA20_BIAS_MAX) / PARAMS.MA20_BIAS_MAX)
    else:
        bias20, s_ma20 = 0.0, 0

    # B. 站上 MA60（連續）
    if price and ma60:
        bias60 = (price - ma60) / ma60 * 100
        if bias60 >= 0:
            s_ma60 = min(15, 15 * min(bias60, PARAMS.MA60_BIAS_MAX) / PARAMS.MA60_BIAS_MAX)
        else:
            s_ma60 = max(-12, 12 * max(bias60, -PARAMS.MA60_BIAS_MAX) / PARAMS.MA60_BIAS_MAX)
    else:
        bias60, s_ma60 = 0.0, 0

    # C. 量價同向
    if chg >= 0:
        s_vol = min(20, max(0, (vr - 0.8) / 0.7 * 20))  # 0.8~1.5 → 0~20
    else:
        s_vol = -min(5, max(0, (1.0 - vr) / 0.2 * 5)) if vr < 1.0 else 0

    # D. 突破前高（連續）
    prev_high = None
    if len(bars) >= 2:
        recent = bars[-(PARAMS.BREAKOUT_LOOKBACK + 1):-1]
        highs = [b.get("high") for b in recent if b.get("high") is not None]
        prev_high = max(highs) if highs else None
    prev_close = bars[-2].get("close") if len(bars) >= 2 else None
    if price and prev_high:
        dist = (price - prev_high) / prev_high * 100
        if dist >= 0:
            s_brk = 20 + min(5, dist * 5)   # 已突破 +0~1% → 20~25
        else:
            s_brk = max(0, 20 * (1 + dist / PARAMS.BREAKOUT_DIST_MAX))  # -5%~0 → 0~20
        s_brk = min(25, max(0, s_brk))
    else:
        s_brk, dist = 0, 0

    # E. 過度乖離懲罰
    bias_pen = -10 if abs(bias20) >= PARAMS.BIAS_OVER else 0

    s = s_ma20 + s_ma60 + s_vol + s_brk + bias_pen
    ev = {
        "ma20": round(s_ma20, 1),
        "ma60": round(s_ma60, 1),
        "vol_align": round(s_vol, 1),
        "breakout": round(s_brk, 1),
        "bias_pct": round(bias20, 1),
        "prev_high": prev_high,
        "prev_close": prev_close,
        "breakout_lookback": PARAMS.BREAKOUT_LOOKBACK,
        "ma20_val": ma20,
        "ma60_val": ma60,
    }
    return _clip(s), ev, quality


def score_capital(snap: Dict, aflow_ratio: Optional[float], flow_source: str) -> tuple:
    """
    資金分（盤後估算）：
      - 若 eod_snapshot 有 aflow_ratio 則直接使用
      - 否則以 change_rate × volume_ratio 估算（漲時量增為正）
      - 方向 ±20 分，無方向則 0
    回傳 (score, evidence, quality)
    """
    if aflow_ratio is None:
        # 盤後估算：漲時量增→流入；跌時量增→流出
        chg = snap.get("change_rate") or 0
        vr = snap.get("volume_ratio") or 1.0
        est = chg * (vr - 1.0) / 10  # 簡單代理
        aflow_ratio = max(-0.3, min(0.3, est))
        flow_source = "eod_estimated"
        quality = "degraded"
    else:
        quality = "ok"

    s = 50
    if aflow_ratio >= PARAMS.FLOW_EPS:
        s += 20
    elif aflow_ratio <= -PARAMS.FLOW_EPS:
        s -= 15

    ev = {"real": 1 if flow_source == "intraday_aflow" else 0,
          "aflow_ratio": round(aflow_ratio, 4)}
    return _clip(s), ev, quality


def score_chip(chip_data: Optional[Dict]) -> tuple:
    """
    籌碼分（讀取 chip_provider 或資料庫緩存）：
      - 無資料 → 回傳 (None, None, "籌碼待補", {}, "pending")
      - 有資料 → 按法人淨買超、連買天數、大戶趨勢打分
    回傳 (score, chip_ok, note, evidence, quality)
    """
    if not chip_data:
        return None, None, "籌碼待補", {"net20": None, "streak": None, "big": None}, "pending"

    net = chip_data.get("inst_net_20d_lots")
    streak = chip_data.get("inst_streak")
    big = chip_data.get("big_holder_trend")

    if net is None and streak is None and big is None:
        return None, None, "無籌碼資料", {"net20": None, "streak": None, "big": None}, "missing"

    s = 50
    if net is not None:
        s += 18 if net > 0 else -15
    if streak is not None:
        s += min(12, streak * 3) if streak > 0 else max(-12, streak * 3)
    if big is not None:
        s += 10 if big > 0 else (-8 if big < 0 else 0)

    pos = ((net or 0) > 0) or ((streak or 0) >= 3) or ((big or 0) > 0)
    neg = ((net or 0) < 0 and (streak or 0) <= -3)
    chip_ok = 1 if (pos and not neg) else (0 if neg else None)

    note = f"三大法人20日合計{(net or 0):+,}張"
    if streak:
        note += f",外資連{'買' if streak > 0 else '賣'}{abs(streak)}日"
    if big is not None:
        note += f",大戶{big:+.1f}pp"
    ev = {"net20": net, "streak": streak, "big": big}
    return _clip(s), chip_ok, note, ev, "ok"


def score_sector(chg: float, sector_pct: float, sector_rank: Optional[int] = None) -> tuple:
    """
    族群分：相對族群強弱為主（領先加分）＋ 族群本身動能。
    """
    rel = round(chg - (sector_pct or 0), 2)
    s = 55
    s += 20 if rel > 0 else (-15 if rel < 0 else 0)
    s += 10 if (sector_pct or 0) > 0 else -5
    if sector_rank is not None:
        s += max(0, 10 - (sector_rank - 1) * 2)
    ev = {"chg": chg, "sector_pct": sector_pct, "relative": rel, "rank": sector_rank}
    return _clip(s), rel, ev, "ok"


# ═══════════════════════════════════════════════════════════
# 二、象限／趨勢
# ═══════════════════════════════════════════════════════════
QUAD_RANK = {"out_down": 0, "in_down": 1, "out_up": 2, "in_up": 3}


def quadrant(aflow_ratio: float, chg: float) -> str:
    flow_in = aflow_ratio >= 0
    if flow_in and chg >= 0:
        return "in_up"
    if flow_in:
        return "in_down"
    return "out_up" if chg >= 0 else "out_down"


def trend_of(quad: str, prev_quad: Optional[str]) -> str:
    if not prev_quad:
        return "新增"
    d = QUAD_RANK[quad] - QUAD_RANK.get(prev_quad, 1)
    return "改善" if d > 0 else ("惡化" if d < 0 else "持平")


# ═══════════════════════════════════════════════════════════
# 三、風險旗標（硬／軟）
# ═══════════════════════════════════════════════════════════
def _tick_size(price: float) -> float:
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def _limit_up_price(prev_close: Optional[float]) -> Optional[float]:
    """台股漲停價：昨收 × 1.1 後依價格級距向下取整至跳動單位。"""
    if prev_close is None or prev_close <= 0:
        return None
    raw = float(prev_close) * 1.10
    tick = _tick_size(raw)
    return math.floor(raw / tick + 1e-9) * tick


def _close_near_limit(snap: Dict, price: Optional[float]) -> bool:
    if price is None:
        return False
    limit_up = snap.get("limit_up") or snap.get("limit_up_price")
    if limit_up is None:
        limit_up = _limit_up_price(snap.get("prev_close"))
    return bool(limit_up and float(price) >= float(limit_up) * PARAMS.NEAR_LIMIT_CLOSE_PCT)


def risk_flags(snap: Dict, tech_ev: Dict, cap_ev: Dict, data_quality: Dict) -> Dict:
    price = snap.get("price") or snap.get("close")
    chg = snap.get("change_rate") or 0
    vr = snap.get("volume_ratio") or 0
    ma20 = tech_ev.get("ma20_val")
    prev_high = tech_ev.get("prev_high")

    # 硬風險
    ma_break = int(price is not None and ma20 is not None and price < ma20)
    divergence = int((chg > 0 and vr < 0.8) or (chg < 0 and vr >= 1.5))
    proxy = int(cap_ev.get("real") == 0)

    # 軟風險
    over_bias = int(abs(tech_ev.get("bias_pct") or 0) >= PARAMS.BIAS_OVER)
    near_limit_snap = snap if snap.get("prev_close") is not None else {
        **snap, "prev_close": tech_ev.get("prev_close")
    }
    near_limit = int(_close_near_limit(near_limit_snap, price))
    no_breakout = int((tech_ev.get("breakout") or 0) < 20)
    resistance = int(price is not None and prev_high is not None and prev_high > 0 and
                     0 <= (prev_high - price) / prev_high * 100 <= PARAMS.RESIST_NEAR)
    data_incomplete = int(any(v != "ok" for v in data_quality.values()))
    net_active_missing = int(data_quality.get("capital") != "ok")

    return {
        "ma_break": ma_break,
        "divergence": divergence,
        "proxy": proxy,
        "over_bias": over_bias,
        "near_limit": near_limit,
        "no_breakout": no_breakout,
        "resistance": resistance,
        "data_incomplete": data_incomplete,
        "net_active_missing": net_active_missing,
    }


def hard_hits(risk: Dict) -> List[str]:
    names = {"ma_break": "跌破 MA20", "divergence": "量價背離", "proxy": "資金為代理",
             "net_active_missing": "缺 net_active"}
    return [v for k, v in names.items() if risk.get(k)]


# ═══════════════════════════════════════════════════════════
# 四、分級＋硬風險封頂
# ═══════════════════════════════════════════════════════════
def grade_and_reason(health: int, quad: str, chip_ok: Optional[int],
                     risk: Dict, track: str, above_ma20: bool = False) -> tuple:
    hard = hard_hits(risk)
    if risk.get("net_active_missing"):
        return "Watch", True, "DATA_INCOMPLETE｜缺 net_active，禁止正式進場", hard
    if track == "engine":
        base = "Ready" if (above_ma20 and health >= PARAMS.WATCH_MIN and chip_ok != 0) \
            else ("Watch" if (above_ma20 or health >= PARAMS.WATCH_MIN) else "Hold")
    else:
        if health >= PARAMS.READY_MIN and quad == "in_up" and chip_ok != 0:
            base = "Ready"
        elif health >= PARAMS.WATCH_MIN or (quad == "in_down"):
            base = "Watch"
        else:
            base = "Hold"

    capped = False
    grade = base
    if base == "Ready" and hard:
        grade, capped = "Watch", True

    if grade == "Ready":
        bits = ["健康分達標"]
        if quad == "in_up":
            bits.append("真實資金流入")
        if chip_ok == 1:
            bits.append("籌碼蓋章")
        if above_ma20:
            bits.append("站上月線")
        reason = "+".join(bits)
    elif capped:
        reason = f"分數達標,但命中硬風險({'、'.join(hard)})→封頂"
    elif base == "Watch":
        if chip_ok is None:
            reason = "分數達標,但籌碼尚未蓋章"
        elif quad == "in_down":
            reason = "流入但收跌,待驗證是否假紅"
        else:
            reason = "接近門檻,趨勢待轉強"
    else:
        reason = "資金流出或分數不足,暫不動作"
    return grade, capped, reason, hard


# ═══════════════════════════════════════════════════════════
# 五、交易計畫（ATR）+ AI 勝率
# ═══════════════════════════════════════════════════════════
def trade_plan(snap: Dict, bars: List[Dict], tech_ev: Dict, track: str) -> Dict:
    price = snap.get("price") or snap.get("close") or 0
    atr = _atr(bars) or (price * 0.02)
    prev_high = tech_ev.get("prev_high")
    trigger = round(prev_high, 1) if (track == "attack" and prev_high) else round(price, 1)
    stop = round(trigger - atr * PARAMS.ATR_STOP_MULT, 1)
    target = round(trigger + atr * PARAMS.ATR_TGT_MULT, 1)
    return {"trigger": trigger, "stop": stop, "target": target, "vol": "需>昨"}


def ai_winrate(health: int, risk: Dict, chip_ok: Optional[int]) -> Dict:
    d1 = PARAMS.AI_BASE_D1 + health * 0.5
    d5 = PARAMS.AI_BASE_D5 + health * 0.62
    if chip_ok == 1:
        d1 += 4
        d5 += 5
    if chip_ok == 0:
        d1 -= 8
        d5 -= 10
    hard_cnt = len(hard_hits(risk))
    d1 -= 6 * hard_cnt
    d5 -= 5 * hard_cnt
    return {"d1": _clip(d1, 5, 95), "d5": _clip(d5, 5, 95)}


# ═══════════════════════════════════════════════════════════
# 六、單檔組裝 → 前端一整包
# ═══════════════════════════════════════════════════════════
def build_row(snap: Dict, *,
              bars: Optional[List[Dict]] = None,
              chip_data: Optional[Dict] = None,
              prev_quad: Optional[str] = None,
              aflow_ratio: Optional[float] = None,
              flow_source: str = "eod_estimated",
              sector_pct: float = 0.0,
              sector_rank: Optional[int] = None,
              health_rank: Optional[str] = None,
              track: Optional[str] = None) -> Dict:
    """組裝單檔全部欄位，對齊前端 money_health_table.html"""
    bars = bars or []
    code = snap.get("code")
    name = snap.get("name") or code
    if C and hasattr(C, "NAME_MAP"):
        name = C.NAME_MAP.get(code, name)
    sector = snap.get("sector")
    if not sector and C and hasattr(C, "SECTOR_MAP") and code in C.SECTOR_MAP:
        sector, _ = C.SECTOR_MAP[code]
    if track is None:
        track = "engine" if (C and code in getattr(C, "ENGINE_STOCKS", set())) else "attack"

    chg = snap.get("change_rate") or 0

    # 四模組計分
    t_s, t_ev, t_q = score_technical(snap, bars)
    c_s, c_ev, c_q = score_capital(snap, aflow_ratio, flow_source)
    ch_s, chip_ok, chip_note, ch_ev, ch_q = score_chip(chip_data)
    se_s, rel, se_ev, se_q = score_sector(chg, sector_pct, sector_rank)

    data_quality = {
        "technical": t_q,
        "capital": c_q,
        "chip": ch_q,
        "sector": se_q,
    }

    # 健康分合成（chip 缺資料時自動重分配權重）
    parts = [
        ("tech", t_s, PARAMS.W_TECH),
        ("cap", c_s, PARAMS.W_CAP),
        ("chip", ch_s, PARAMS.W_CHIP),
        ("sector", se_s, PARAMS.W_SECTOR),
    ]
    valid = [(n, v, w) for n, v, w in parts if v is not None]
    if not valid:
        health = 50
    else:
        wsum = sum(w for _, _, w in valid)
        health = _clip(sum(v * (w / wsum) for _, v, w in valid))

    # 象限與趨勢
    aflow = c_ev.get("aflow_ratio", 0.0)
    quad = quadrant(aflow, chg)
    trend = trend_of(quad, prev_quad)

    # 風險
    risk = risk_flags(snap, t_ev, c_ev, data_quality)
    above_ma20 = bool(t_ev.get("ma20_val") is not None and snap.get("price") and snap["price"] >= t_ev["ma20_val"])

    # 分級
    grade, capped, reason, hard = grade_and_reason(
        health, quad, chip_ok, risk, track, above_ma20=above_ma20
    )

    # 計畫與 AI
    plan = trade_plan(snap, bars, t_ev, track)
    ai = ai_winrate(health, risk, chip_ok)

    return {
        "code": code,
        "name": name,
        "sector": sector,
        "track": track,
        "change_rate": round(chg, 2),
        "health_score": health,
        "technical_score": t_s,
        "capital_score": c_s,
        "chip_score": ch_s,
        "sector_score": se_s,
        "quadrant": quad,
        "prev_quadrant": prev_quad,
        "flow_streak": 1,  # 盤後無連續天數，預設 1
        "trend": trend,
        "chip_ok": chip_ok,
        "chip_note": chip_note,
        "relative_vs_sector": rel,
        "sector_pct": round(sector_pct, 2),
        "flow_source": flow_source,
        "data_quality": data_quality,
        "tech_ev": {
            "ma20": t_ev.get("ma20"),
            "ma60": t_ev.get("ma60"),
            "vol_align": t_ev.get("vol_align"),
            "breakout": t_ev.get("breakout"),
            "bias_pct": t_ev.get("bias_pct"),
        },
        "cap_ev": c_ev,
        "chip_ev": ch_ev,
        "seq5": [],
        "risk": risk,
        "plan": plan,
        "ai": ai,
        "grade": grade,
        "grade_reason": reason,
        "data_status": "DATA_INCOMPLETE" if risk.get("net_active_missing") else "OK",
        "_capped": capped,
    }


# ═══════════════════════════════════════════════════════════
# 七、FastAPI Router（純盤後，只讀資料庫）
# ═══════════════════════════════════════════════════════════
try:
    from fastapi import APIRouter
    router = APIRouter()
    _CACHE = {}

    @router.get("/api/mh/overview")
    def api_overview(date: str = ""):
        """盤後資金健康度總覽，數據完全來自 mls.db"""
        tdate = date or _today()
        cached = _CACHE.get(tdate)
        if cached:
            return cached

        # 1. 讀取 EOD 快照
        snaps = _read_eod_snapshots(tdate)
        if not snaps:
            # 若無今日資料，取最新一日
            snaps = _read_eod_snapshots()
            if snaps:
                # 更新 tdate 為實際日期
                first = snaps[0]
                if "source_date" in first:
                    tdate = str(first["source_date"])[:10]
                elif "trade_date" in first:
                    tdate = str(first["trade_date"])[:10]

        if not snaps:
            return {"date": tdate, "source": "eod", "rows": []}

        # 2. 計算族群平均漲跌幅
        groups = {}
        for s in snaps:
            sector = s.get("sector")
            chg = s.get("change_rate")
            if sector and isinstance(chg, (int, float)):
                groups.setdefault(sector, []).append(chg)
        sector_pct_map = {
            name: round(sum(vals) / len(vals), 2)
            for name, vals in groups.items() if vals
        }

        # 3. 逐檔組裝（讀取 K 線 + 籌碼）
        rows = []
        for s in snaps:
            code = s.get("code")
            if not code:
                continue

            # 讀取 K 線（盤後固定讀 70 日）
            bars = _read_daily_bars(code, days=70, asof=tdate)

            # 讀取籌碼（若有 chip_provider）
            chip_data = None
            if chip_provider is not None:
                try:
                    _chip = chip_provider.get_chip_data(code)
                    # chip_provider.get_chip_data 回 (data_dict, quality_str) tuple
                    if isinstance(_chip, tuple) and len(_chip) >= 1:
                        chip_data = _chip[0]
                    elif isinstance(_chip, dict):
                        chip_data = _chip
                except Exception:
                    pass

            # 從 snap 讀取 aflow_ratio（若有）
            aflow = s.get("aflow_ratio")
            flow_source = "intraday_aflow" if aflow is not None else "eod_estimated"

            row = build_row(
                snap=s,
                bars=bars,
                chip_data=chip_data,
                aflow_ratio=aflow,
                flow_source=flow_source,
                sector_pct=sector_pct_map.get(s.get("sector"), 0.0),
            )
            rows.append(row)

        # 排序：Ready → Watch → Hold，同級健康分高→低
        gord = {"Ready": 0, "Watch": 1, "Hold": 2}
        rows.sort(key=lambda r: (gord.get(r["grade"], 9), -r["health_score"]))

        payload = {"date": tdate, "source": "eod", "rows": rows}
        _CACHE[tdate] = payload
        return payload

except Exception as e:
    router = None
    print(f"[money_health_api] 路由載入失敗（離線環境）: {e}")


# ═══════════════════════════════════════════════════════════
# 八、離線測試（python money_health_api.py）
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    # 模擬 K 線（站上均線情境）
    def make_bars_above(price, n=65):
        bars, p = [], price * 0.75
        for i in range(n):
            p *= 1 + 0.004
            bars.append({"date": f"d{i}", "close": round(p, 1),
                         "high": round(p * 1.01, 1), "low": round(p * 0.99, 1)})
        bars.append({"date": "last", "close": round(price, 1),
                     "high": round(price * 1.005, 1), "low": round(price * 0.995, 1)})
        return bars

    def make_bars_below(price, n=65):
        bars, p = [], price * 1.35
        for i in range(n):
            p *= 1 - 0.005
            bars.append({"date": f"d{i}", "close": round(p, 1),
                         "high": round(p * 1.01, 1), "low": round(p * 0.99, 1)})
        bars.append({"date": "last", "close": round(price, 1),
                     "high": round(price * 1.005, 1), "low": round(price * 0.995, 1)})
        return bars

    # 測試案例 A：站上均線 + 資金流入 + 籌碼正向 → Ready
    A = build_row(
        {"code": "5347", "name": "世界先進", "sector": "晶圓代工",
         "price": 99.2, "change_rate": 1.9, "volume_ratio": 1.3},
        bars=make_bars_above(99.2),
        chip_data={"inst_net_20d_lots": 2020, "inst_streak": 4, "big_holder_trend": 0.2},
        aflow_ratio=0.06,
        flow_source="intraday_aflow",
        sector_pct=1.2,
        track="attack",
    )

    # 測試案例 B：破線 + 資金流出 + 籌碼反向 → Hold
    B = build_row(
        {"code": "8150", "name": "南茂", "sector": "封測",
         "price": 39.8, "change_rate": -3.1, "volume_ratio": 1.6},
        bars=make_bars_below(39.8),
        chip_data={"inst_net_20d_lots": -1540, "inst_streak": -4, "big_holder_trend": -0.4},
        aflow_ratio=-0.05,
        flow_source="intraday_aflow",
        sector_pct=-2.2,
        track="attack",
    )

    print("=== A（站均線+資金流入+籌碼正）===")
    print(f"健康分 {A['health_score']}  技{A['technical_score']} 資{A['capital_score']} 籌{A['chip_score']} 族{A['sector_score']}")
    print(f"分級 {A['grade']}  理由:{A['grade_reason']}")
    print(f"風險: {[k for k,v in A['risk'].items() if v]}")

    print("\n=== B（破線+資金流出+籌碼負）===")
    print(f"健康分 {B['health_score']}  技{B['technical_score']} 資{B['capital_score']} 籌{B['chip_score']} 族{B['sector_score']}")
    print(f"分級 {B['grade']}  理由:{B['grade_reason']}")
    print(f"風險: {[k for k,v in B['risk'].items() if v]}")

    assert A["grade"] == "Ready", f"A 應為 Ready，實得 {A['grade']}"
    assert B["grade"] == "Hold", f"B 應為 Hold，實得 {B['grade']}"
    print("\n✅ 離線測試通過：分數有區別度，分級正確")
