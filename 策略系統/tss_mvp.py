"""
tss_mvp.py — TSS v1.0 主動買賣盤四因子進場策略 (MVP 骨架)

目的:
    把 TSS v1.0 規格書的「盤後篩選模組」四因子濾網
    先做成可獨立驗證的 MVP,不動主系統 engine.py / scoring.py 主邏輯。

邊界:
    - 純計算 + 篩選,不寫下單
    - 對接 Shioaji 1.5.5 (api_key/secret_key + Decimal price)
    - 可在 --dry-run 模式跑 (mock 資料,不登入券商)
    - 不動主系統檔案

對應規格書章節:
    - 三、 核心指標定義: Buy/Sell Vol 計算 (3.1) + MA20 + 千張大戶 + 三大法人 (3.2)
    - 四、 盤後篩選模組: 四大進場濾網 (條件1~4)
    - 六、 強制停止進場條件: 預留 hooks
    - 七、 程式碼模組架構: ShioajiActiveVolumeTracker 介面對齊
"""

from __future__ import annotations

import os
import time
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

import pandas as pd
import numpy as np


# ============================================================
# 1. 參數常數 (對應規格書第八章)
# ============================================================
@dataclass(frozen=True)
class TSSParams:
    """TSS v1.0 全部參數集中放這邊,方便之後做參數搜尋。"""

    # --- 條件 1: 市場系統面 ---
    market_ma_window: int = 20  # MA20
    market_require_above_ma20: bool = True
    market_require_ma20_rising: bool = True  # MA20 > 昨日 MA20

    # --- 條件 2: 籌碼共鳴面 ---
    big_holder_require_wow: bool = True  # 本週千張大戶 > 上週
    inst_require_at_least_one_buy: bool = True  # 三大法人至少一買

    # --- 條件 3: 技術發動面 ---
    stock_ma_window: int = 20
    stock_bias_pct_max: float = 3.0  # 乖離率上限 3%
    vol_ratio_vs_5d: float = 1.0  # 量 > 5日均量
    require_break_prev_high: bool = True  # 收盤 > 昨日最高

    # --- 條件 4: 主動買賣盤 ---
    bs_ratio_threshold: float = 1.25  # Buy/Sell > 1.25

    # --- 六、強制停止進場條件 ---
    force_stop_index_drop_pct: float = 1.5  # 指數跌 -1.5%
    force_stop_volume_spike_ratio: float = 10.0  # 5分K量 > 昨日 10%
    earnings_blackout_days: int = 7  # 財報空窗期


PARAMS = TSSParams()


# ============================================================
# 2. ShioajiActiveVolumeTracker (規格書 7.1)
# ============================================================
@dataclass
class ActiveVolumeSnapshot:
    """盤中某一刻的主動買賣盤快照"""
    total_buy_vol: int
    total_sell_vol: int
    bs_ratio_full: float  # 全天累積比
    bs_ratio_5min: float  # 近5分鐘比
    last_tick_price: float
    last_tick_ts: datetime


class ShioajiActiveVolumeTracker:
    """
    即時運算 Buy/Sell Vol 的小工具 (規格書 7.1)。

    用途:
        - 盤中模組餵 tick 進來,自動累計 Buy/Sell Vol
        - 維持近 5 分鐘 tick history,供 BS_Ratio_5min 計算
        - 若 bid/ask 為 None (五檔缺),改用「價格跳動方向」fallback

    注: TickSTKv1 namedtuple 必須提供 .code / .close / .volume / .bid_price / .ask_price / .datetime
       若欄位缺失,會用 fallback (price change direction)。
    """

    def __init__(self, code: str):
        self.code = code
        self.total_buy_vol = 0
        self.total_sell_vol = 0
        self._last_close: Optional[float] = None
        self._tick_history: deque = deque(maxlen=2000)  # 1分K下 5分鐘夠

    def add_tick(self, tick) -> None:
        """
        餵入單筆 tick (Shioaji TickSTKv1 或 mock)。
        判斷 Buy/Sell 規則 (規格書 3.1 即時定義):
          - tick.close >= ask_price -> Buy Vol
          - tick.close <= bid_price -> Sell Vol
          - 兩者都 None -> 用 last close 比較 (close > prev -> Buy, close < prev -> Sell)
        """
        try:
            price = float(tick.close)
        except (AttributeError, TypeError):
            return

        vol = int(getattr(tick, "volume", 0) or 0)
        bid = getattr(tick, "bid_price", None)
        ask = getattr(tick, "ask_price", None)

        classified = False

        # 五檔正常: 走規格書 3.1 即時定義
        if ask is not None and float(ask) > 0 and price >= float(ask):
            self.total_buy_vol += vol
            classified = True
        elif bid is not None and float(bid) > 0 and price <= float(bid):
            self.total_sell_vol += vol
            classified = True

        # 五檔缺: fallback 用 last close 比較 (規格書 9.「價格跳動方向備援」)
        if not classified and self._last_close is not None:
            if price > self._last_close:
                self.total_buy_vol += vol
                classified = True
            elif price < self._last_close:
                self.total_sell_vol += vol
                classified = True

        self._last_close = price
        tick_ts = getattr(tick, "datetime", datetime.now())
        self._tick_history.append({
            "ts": tick_ts,
            "price": price,
            "volume": vol,
        })

    def get_5min_buy(self) -> int:
        cutoff = datetime.now() - timedelta(minutes=5)
        return sum(t["volume"] for t in self._tick_history
                   if t["ts"] >= cutoff and t["price"] >= self._last_close)

    def get_5min_sell(self) -> int:
        cutoff = datetime.now() - timedelta(minutes=5)
        return sum(t["volume"] for t in self._tick_history
                   if t["ts"] >= cutoff and t["price"] < self._last_close)

    def snapshot(self) -> ActiveVolumeSnapshot:
        buy5 = self.get_5min_buy()
        sell5 = self.get_5min_sell()
        full = (self.total_buy_vol + self.total_sell_vol) or 1
        five = (buy5 + sell5) or 1
        return ActiveVolumeSnapshot(
            total_buy_vol=self.total_buy_vol,
            total_sell_vol=self.total_sell_vol,
            bs_ratio_full=self.total_buy_vol / full,
            bs_ratio_5min=buy5 / five,
            last_tick_price=self._last_close or 0.0,
            last_tick_ts=datetime.now(),
        )


# ============================================================
# 3. 歷史資料擷取: fetch_1min_kbars (規格書 7.2)
# ============================================================
def fetch_1min_kbars(api, contract, start: datetime, end: datetime) -> pd.DataFrame:
    """
    抓取歷史 1 分鐘 K 棒,自動 5 天分段 (規格書 7.2 + 9)。
    Shioaji 1.5.5 attribute-style 寫法:kb.ts / kbar.Close (不是 dict)。
    """
    import shioaji as sj

    rows = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=5), end)
        print(f"  → 抓 {cursor.date()} ~ {chunk_end.date()}")
        kbars = api.kbars(
            contract=contract,
            start=cursor.strftime("%Y-%m-%d"),
            end=chunk_end.strftime("%Y-%m-%d"),
        )
        # Shioaji 1.5.5: kbars 是 Struct,可用 dict() 或 pd.DataFrame({**kbars})
        df = pd.DataFrame({**kbars})
        if not df.empty:
            rows.append(df)
        cursor = chunk_end
        time.sleep(0.5)  # 規格書 9: 限流防禦
    if not rows:
        return pd.DataFrame(columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    out = pd.concat(rows, ignore_index=True).drop_duplicates(subset=["ts"])
    out["ts"] = pd.to_datetime(out["ts"])
    return out.sort_values("ts").reset_index(drop=True)


# ============================================================
# 4. Buy/Sell Vol 歷史回測定義 (規格書 3.1 歷史回測定義)
# ============================================================
def classify_buy_sell_vol(kbars_1m: pd.DataFrame) -> pd.DataFrame:
    """
    給 1 分 K 棒,逐根算 Buy_Vol / Sell_Vol。
    規則 (規格書 3.1 歷史回測定義):
      - 該根 Close > Open 或 Close > 前一根收盤 -> 整根量算 Buy
      - 該根 Close < Open 或 Close < 前一根收盤 -> 整根量算 Sell
      - 都相等 (Doji): 量 50/50 拆 (保守做法,跟規格書沒說清楚)
    """
    df = kbars_1m.copy()
    df["prev_close"] = df["Close"].shift(1)
    df["Buy_Vol"] = 0
    df["Sell_Vol"] = 0

    bull_mask = (df["Close"] > df["Open"]) | (df["Close"] > df["prev_close"])
    bear_mask = (df["Close"] < df["Open"]) | (df["Close"] < df["prev_close"])

    df.loc[bull_mask, "Buy_Vol"] = df.loc[bull_mask, "Volume"]
    df.loc[bear_mask, "Sell_Vol"] = df.loc[bear_mask, "Volume"]

    # Doji (平盤): 量 50/50 分 (保守)
    doji_mask = ~bull_mask & ~bear_mask
    df.loc[doji_mask, "Buy_Vol"] = (df.loc[doji_mask, "Volume"] / 2).astype(int)
    df.loc[doji_mask, "Sell_Vol"] = (df.loc[doji_mask, "Volume"] / 2).astype(int)

    return df


# ============================================================
# 5. 盤後篩選四因子 (規格書 第四章)
# ============================================================
def filter_after_market(
    stock_df_1m: pd.DataFrame,
    index_df_daily: Optional[pd.DataFrame] = None,
    big_holder_df: Optional[pd.DataFrame] = None,
    institutional_df: Optional[pd.DataFrame] = None,
    earnings_blackout: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """
    對單檔個股跑「盤後四因子篩選」。

    輸入:
      - stock_df_1m: 1 分 K (已含 Buy_Vol / Sell_Vol)
      - index_df_daily: 加權指數日 K (給條件 1 用),None = 條件1 跳過
      - big_holder_df: 千張大戶比率時序 (給條件 2 用),None = 條件2 跳過
      - institutional_df: 三大法人買賣超 (給條件 2 用),None = 條件2 跳過
      - earnings_blackout: {code: True/False} 財報空窗旗標,True = 跳過

    輸出: dict 含每個條件是否通過 + 最終 Final_Signal + 當日 BS Ratio
    """
    if stock_df_1m.empty:
        return {"pass_all": False, "reason": "no_data"}

    # 日級聚合
    df = stock_df_1m.copy()
    df["Date"] = df["ts"].dt.date
    daily = df.groupby("Date").agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
        Buy_Vol=("Buy_Vol", "sum"),
        Sell_Vol=("Sell_Vol", "sum"),
    ).reset_index()

    daily["MA20"] = daily["Close"].rolling(PARAMS.stock_ma_window).mean()
    daily["MA5_Vol"] = daily["Volume"].rolling(5).mean()
    daily["Bias_pct"] = ((daily["Close"] - daily["MA20"]) / daily["MA20"] * 100).round(2)
    daily["BS_Ratio_Daily"] = (
        daily["Buy_Vol"] / daily["Sell_Vol"].replace(0, np.nan)
    ).fillna(0).round(3)

    # 取最新交易日
    last = daily.iloc[-1]
    prev = daily.iloc[-2] if len(daily) >= 2 else None

    result: Dict[str, Any] = {
        "trade_date": str(last["Date"]),
        "close": float(last["Close"]),
        "ma20": float(last["MA20"]) if not pd.isna(last["MA20"]) else None,
        "bias_pct": float(last["Bias_pct"]) if not pd.isna(last["Bias_pct"]) else None,
        "buy_vol": int(last["Buy_Vol"]),
        "sell_vol": int(last["Sell_Vol"]),
        "bs_ratio_daily": float(last["BS_Ratio_Daily"]),
        "conditions": {},
    }

    # --- 條件 1: 市場系統面 ---
    cond1 = True
    if index_df_daily is not None and not index_df_daily.empty:
        idx = index_df_daily.copy()
        idx["MA20"] = idx["Close"].rolling(PARAMS.market_ma_window).mean()
        idx_last = idx.iloc[-1]
        idx_prev = idx.iloc[-2] if len(idx) >= 2 else None
        cond1_above_ma20 = idx_last["Close"] > idx_last["MA20"]
        cond1_ma20_rising = (
            idx_prev is not None and idx_last["MA20"] > idx_prev["MA20"]
        )
        cond1 = cond1_above_ma20 and cond1_ma20_rising
        result["conditions"]["C1_market"] = {
            "above_ma20": bool(cond1_above_ma20),
            "ma20_rising": bool(cond1_ma20_rising),
            "pass": bool(cond1),
        }
    else:
        result["conditions"]["C1_market"] = {"pass": True, "skipped": "no_index_data"}

    # --- 條件 2: 籌碼共鳴面 ---
    cond2 = True
    if big_holder_df is not None and not big_holder_df.empty:
        bh = filter_after_market_big_holder(big_holder_df)
        cond2 = bh["ratio_wow_up"]
        result["conditions"]["C2_chips_big_holder"] = {
            "ratio_wow_up": bh["ratio_wow_up"],
            "latest_ratio": bh["latest_ratio"],
            "prev_ratio": bh["prev_ratio"],
            "pass": bool(cond2),
        }
    else:
        result["conditions"]["C2_chips_big_holder"] = {"pass": True, "skipped": "no_big_holder_data"}

    if institutional_df is not None and not institutional_df.empty:
        inst_last = institutional_df.iloc[-1]
        at_least_one_buy = (inst_last.get("foreign_buy", 0) > 0) or (inst_last.get("trust_buy", 0) > 0)
        cond2 = cond2 and at_least_one_buy
        result["conditions"]["C2_chips_inst"] = {
            "at_least_one_buy": bool(at_least_one_buy),
            "pass": bool(at_least_one_buy),
        }
    else:
        result["conditions"]["C2_chips_inst"] = {"pass": True, "skipped": "no_inst_data"}

    # --- 條件 3: 技術發動面 ---
    cond3_above_ma20 = last["Close"] > last["MA20"] if last["MA20"] else False
    cond3_bias_ok = (
        last["Bias_pct"] is not None
        and last["Bias_pct"] <= PARAMS.stock_bias_pct_max
    )
    cond3_vol_ok = last["Volume"] > last["MA5_Vol"] if last["MA5_Vol"] else False
    cond3_break_high = True
    if prev is not None:
        cond3_break_high = last["Close"] > prev["High"]
    cond3 = cond3_above_ma20 and cond3_bias_ok and cond3_vol_ok and cond3_break_high
    result["conditions"]["C3_technical"] = {
        "above_ma20": bool(cond3_above_ma20),
        "bias_ok": bool(cond3_bias_ok),
        "vol_ok": bool(cond3_vol_ok),
        "break_prev_high": bool(cond3_break_high),
        "pass": bool(cond3),
    }

    # --- 條件 4: 主動買賣盤 ---
    cond4 = last["BS_Ratio_Daily"] > PARAMS.bs_ratio_threshold
    result["conditions"]["C4_active_vol"] = {
        "bs_ratio_daily": float(last["BS_Ratio_Daily"]),
        "threshold": PARAMS.bs_ratio_threshold,
        "pass": bool(cond4),
    }

    # --- 六、強制停止進場條件 ---
    force_stop = False
    force_stop_reason = None
    if earnings_blackout and earnings_blackout.get("code", False):
        force_stop = True
        force_stop_reason = "earnings_blackout"

    final = cond1 and cond2 and cond3 and cond4 and not force_stop

    result["final_signal"] = bool(final)
    result["force_stop"] = force_stop
    result["force_stop_reason"] = force_stop_reason
    result["pass_all"] = bool(final)

    return result


# ============================================================
# 6. Mock 工具 (給 --dry-run 用,免登入券商)
# ============================================================
def generate_mock_1m_kbars(
    days: int = 30,
    base_price: float = 600.0,
    seed: int = 42,
) -> pd.DataFrame:
    """
    生假 1 分 K (給 dry-run / 沒登入時驗證流程用)。
    不是要模擬真實市場,是讓篩選邏輯可被 end-to-end 跑一遍。
    """
    np.random.seed(seed)
    rows = []
    start = datetime.now() - timedelta(days=days)
    for d in range(days):
        day = start + timedelta(days=d)
        # 一天約 270 根 1 分 K (09:00~13:30)
        price = base_price + np.random.normal(0, 5)
        for m in range(270):
            ts = day.replace(hour=9, minute=0, second=0) + timedelta(minutes=m)
            change = np.random.normal(0, 0.3)
            o = price
            c = price + change
            h = max(o, c) + abs(np.random.normal(0, 0.2))
            l = min(o, c) - abs(np.random.normal(0, 0.2))
            v = max(1, int(np.random.normal(500, 200)))
            rows.append([ts, o, h, l, c, v])
            price = c
    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    return df


# ============================================================
# 6.5 大盤日 K (規格書 C1 條件 1 市場系統面)
# ============================================================
def fetch_index_daily(api, days: int = 120) -> pd.DataFrame:
    """
    抓加權指數 (TSE001) 日 K,給條件 1 用。
    用 Shioaji Contracts.Indexs.TSE["001"] 對齊 broker.py index_snapshot 的寫法。

    回傳 DataFrame: ts / Open / High / Low / Close / Volume
    """
    contract = api.Contracts.Indexs.TSE["001"]
    end = datetime.now()
    start = end - timedelta(days=days)

    kbars = api.kbars(
        contract=contract,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
    )
    df = pd.DataFrame({**kbars})
    if df.empty:
        return pd.DataFrame(columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    df["ts"] = pd.to_datetime(df["ts"])
    return df.sort_values("ts").reset_index(drop=True)


def mock_index_daily(days: int = 120, seed: int = 7) -> pd.DataFrame:
    """給 dry-run 用,生加權指數 mock 日 K。"""
    np.random.seed(seed)
    rows = []
    base = 22000.0
    end = datetime.now()
    start = end - timedelta(days=days)
    price = base
    for d in range(days):
        day = start + timedelta(days=d)
        change = np.random.normal(0, 100)
        o = price
        c = price + change
        h = max(o, c) + abs(np.random.normal(0, 50))
        l = min(o, c) - abs(np.random.normal(0, 50))
        v = max(1, int(np.random.normal(5_000_000_000, 1_000_000_000)))
        rows.append([day, o, h, l, c, v])
        price = c
    return pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])


# ============================================================
# 6.6 三大法人買賣超 (規格書 C2 條件 2 籌碼共鳴面)
# ============================================================
def fetch_institutional(code: str, days: int = 30) -> pd.DataFrame:
    """
    抓三大法人買賣超 (日),給條件 2 用。
    走 FinMind dataset `TaiwanStockInstitutionalInvestorsBuySell`,
    不動主系統 chips.py,自己開一個 urllib 直連 (避免動到主邏輯)。
    回傳 DataFrame:
      date / foreign_buy (張) / trust_buy (張) / dealer_buy (張)
      / foreign_net / trust_net / dealer_net
    """
    import urllib.request
    import urllib.parse
    import json as json_lib

    token = os.environ.get("FINMIND_TOKEN", "")
    start = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d")
    q = urllib.parse.urlencode({
        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id": code,
        "start_date": start,
    })
    url = f"https://api.finmindtrade.com/api/v4/data?{q}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json_lib.loads(r.read().decode()).get("data", [])
    except Exception as e:
        print(f"[tss] FinMind 法人 {code} 抓取失敗: {e}")
        return pd.DataFrame()

    if not data:
        return pd.DataFrame()

    rows = []
    by_date: Dict[str, Dict[str, float]] = {}
    for r in data:
        name = r.get("name", "")
        if name not in ("Foreign_Investor", "Investment_Trust", "Dealer"):
            continue
        d = r["date"]
        net = (r.get("buy", 0) - r.get("sell", 0)) / 1000  # 股→張
        by_date.setdefault(d, {
            "foreign_net": 0.0, "trust_net": 0.0, "dealer_net": 0.0,
        })
        key = {
            "Foreign_Investor": "foreign_net",
            "Investment_Trust": "trust_net",
            "Dealer": "dealer_net",
        }[name]
        by_date[d][key] += net

    for d in sorted(by_date.keys())[-days:]:
        rows.append({
            "date": d,
            "foreign_net": round(by_date[d]["foreign_net"], 1),
            "trust_net": round(by_date[d]["trust_net"], 1),
            "dealer_net": round(by_date[d]["dealer_net"], 1),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # filter_after_market 裡用 foreign_buy / trust_buy (boolean 用 > 0),命名對齊
    df = df.rename(columns={"foreign_net": "foreign_buy", "trust_net": "trust_buy", "dealer_net": "dealer_buy"})
    return df


def mock_institutional(code: str, days: int = 30, seed: int = 13) -> pd.DataFrame:
    """給 dry-run 用,生法人 mock 日資料。"""
    np.random.seed(seed)
    rows = []
    end = datetime.now()
    start = end - timedelta(days=days)
    for d in range(days):
        day = start + timedelta(days=d)
        # 約 60% 機率外資買超 (個股若走多頭偏這個方向)
        f = round(np.random.normal(500, 3000), 1)
        t = round(np.random.normal(100, 800), 1)
        de = round(np.random.normal(50, 500), 1)
        rows.append({"date": day.strftime("%Y-%m-%d"), "foreign_buy": f, "trust_buy": t, "dealer_buy": de})
    return pd.DataFrame(rows)


# ============================================================
# 6.7 集保千張大戶比例 (規格書 C2 大戶 + 第九章 SOP)
# ============================================================
HERE = Path(os.path.dirname(os.path.abspath(__file__)))
BIG_HOLDER_DATA_DIR = HERE / "data" / "big_holder"

# ── 跟「盤後資金健康度」子系統的對接 ──
# 本地部署: 策略系統/ 跟 盤後資金健康度/ 平級,在「MLS 完整系統 v1.2 /」下
# VPS 部署:  /opt/pos-v1.2/ 是「合併版」,檔案平鋪,沒有「盤後資金健康度/」子資料夾
# 所以用「找檔案」而非「固定子資料夾路徑」做 fallback
_candidates: List[Path] = [
    HERE.parent / "盤後資金健康度",       # 本地:外層子資料夾
    HERE.parent,                          # VPS / 合併版:直接在外層
    HERE,                                 # 極限:跟策略系統同目錄
]


def _find_money_health_dir() -> Path:
    """找出資金健康度檔案所在的目錄 (本地子資料夾 或 VPS 合併版根)。"""
    for base in _candidates:
        if (base / "config.py").exists() and (base / "money_health.py").exists():
            return base
    # fallback:本地預期路徑
    return HERE.parent / "盤後資金健康度"


MONEY_HEALTH_DIR = _find_money_health_dir()
MLS_DB_PATH = HERE.parent / "mls.db"          # 共用主資料庫 (外層)

# .env 候選:本機外層 / 資金健康度/ / 策略系統/
_MLS_DOTENV_CANDIDATES = [
    HERE.parent / ".env",
    MONEY_HEALTH_DIR / ".env",
    HERE / ".env",
]


def _find_dotenv() -> Optional[Path]:
    for p in _MLS_DOTENV_CANDIDATES:
        if p.exists():
            return p
    return None


def load_dotenv_from_money_health() -> None:
    """
    從主系統外層 .env (或資金健康度/.env) 載入金鑰 (跟主系統共用,避免 key 散落各處)。
    不依賴 python-dotenv 套件,自己手刻輕量 parser。
    """
    dotenv = _find_dotenv()
    if dotenv is None:
        return
    try:
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            # 不覆蓋已存在的 env
            os.environ.setdefault(k, v)
    except Exception as e:
        print(f"[tss] 讀 {dotenv.name} 失敗: {e}")


def get_universe() -> List[str]:
    """
    從「盤後資金健康度/config.py」取觀察池 (UNIVERSE)。
    若該子系統不在 (獨立部署),退回 3 檔預設。
    """
    cfg_path = MONEY_HEALTH_DIR / "config.py"
    if not cfg_path.exists():
        return ["2330", "2454", "2603"]
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("mls_money_health_config", cfg_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return list(getattr(mod, "UNIVERSE", ["2330", "2454", "2603"]))
    except Exception as e:
        print(f"[tss] 讀 UNIVERSE 失敗: {e}")
        return ["2330", "2454", "2603"]


def get_chips_from_money_health(code: str) -> Optional[dict]:
    """
    複用「盤後資金健康度/chips.py」的 get_chips() (法人 + 大戶),
    不自己重抓 FinMind,避免重複打 API。
    回傳 None 表示子系統不在或抓不到,呼叫端決定 fallback。
    """
    chips_path = MONEY_HEALTH_DIR / "chips.py"
    if not chips_path.exists():
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("mls_money_health_chips", chips_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.get_chips(code)
    except Exception as e:
        print(f"[tss] 讀 chips {code} 失敗: {e}")
        return None


# 啟動時自動載入 .env
load_dotenv_from_money_health()


def fetch_big_holder(code: str, force_refresh: bool = False) -> pd.DataFrame:
    """
    抓千張大戶比例 (週資料)。
    走 FinMind dataset `TaiwanStockHoldingSharesPer`,
    不從證交所原始 CSV (規格書第九章原始 CSV 流程留 SOP,實作走 FinMind 更穩)。

    三層檔案架構 (規格書第九章):
      1. 原始層: big_holder_raw_<code>_YYYYMMDD.json (FinMind 回傳完整資料)
      2. 清洗層: df_big_holder_<code> (DataFrame,週聚合)
      3. 精簡層: Top1000_ratio_<code>.csv (策略直接讀)

    回傳精簡層 DataFrame:
      trade_date / stock_code / ratio / holders_count

    排程觸發邏輯 (規格書 第九章「實務上條件 2 的運作排程」):
      - 週四 17:00 之後: 抓新一週
      - 週五/六/日/一/二/三: 讀上週已存檔
    """
    import urllib.request
    import urllib.parse
    import json as json_lib

    BIG_HOLDER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = BIG_HOLDER_DATA_DIR / f"Top1000_ratio_{code}.csv"

    # 排程判斷: 若今天是週一到週三,直接讀快取
    today = datetime.now()
    weekday = today.weekday()  # 0=Mon, 3=Thu
    after_5pm = today.hour >= 17
    is_thursday_after_5pm = (weekday == 3 and after_5pm)

    if cache_file.exists() and not (force_refresh or is_thursday_after_5pm):
        # 直接讀上週資料
        return pd.read_csv(cache_file)

    # 抓 FinMind
    token = os.environ.get("FINMIND_TOKEN", "")
    start = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    q = urllib.parse.urlencode({
        "dataset": "TaiwanStockHoldingSharesPer",
        "data_id": code,
        "start_date": start,
    })
    url = f"https://api.finmindtrade.com/api/v4/data?{q}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json_lib.loads(r.read().decode()).get("data", [])
    except Exception as e:
        print(f"[tss] FinMind 集保 {code} 抓取失敗: {e}")
        if cache_file.exists():
            return pd.read_csv(cache_file)  # 失敗 fallback 讀快取
        return pd.DataFrame()

    if not data:
        return pd.DataFrame()

    # 原始層存檔
    raw_file = BIG_HOLDER_DATA_DIR / f"big_holder_raw_{code}_{today.strftime('%Y%m%d')}.json"
    raw_file.write_text(json_lib.dumps(data, ensure_ascii=False), encoding="utf-8")

    # 清洗 + 週聚合:千張大戶 = level 起算 ≥ 1,000,001 股以上 (規格書 BIG_HOLDER_LEVEL=1000 張)
    weeks = {}
    for r in data:
        lvl = str(r.get("HoldingSharesLevel", ""))
        first = lvl.split("-")[0].replace(",", "")
        try:
            min_shares = int(first)
        except ValueError:
            continue
        if min_shares >= 1_000_001:  # 1,000,001 股 = 1000 張 + 1 股 (規格書定義)
            d = r["date"]
            weeks.setdefault(d, {"ratio": 0.0, "holders": 0})
            weeks[d]["ratio"] += float(r.get("percent", 0))
            weeks[d]["holders"] += int(r.get("people", 0))

    if not weeks:
        return pd.DataFrame()

    # 精簡層寫檔
    rows = []
    for d in sorted(weeks.keys()):
        rows.append({
            "trade_date": d,
            "stock_code": code,
            "ratio": round(weeks[d]["ratio"], 4),
            "holders_count": weeks[d]["holders"],
        })
    out = pd.DataFrame(rows)
    out.to_csv(cache_file, index=False)
    return out


def filter_after_market_big_holder(big_holder_df: pd.DataFrame) -> Dict[str, Any]:
    """
    把 big_holder_df 轉成 filter_after_market 期待的格式:
      { ratio_wow_up: bool, latest_ratio: float, prev_ratio: float }

    規格書條件 2 大戶判斷: 本週千張大戶比率 > 上週比率
    """
    if big_holder_df is None or big_holder_df.empty or len(big_holder_df) < 2:
        return {"ratio_wow_up": True, "latest_ratio": None, "prev_ratio": None}

    # 取最近兩週
    sorted_df = big_holder_df.sort_values("trade_date")
    latest = sorted_df.iloc[-1]
    prev = sorted_df.iloc[-2]
    return {
        "ratio_wow_up": bool(latest["ratio"] > prev["ratio"]),
        "latest_ratio": float(latest["ratio"]),
        "prev_ratio": float(prev["ratio"]),
    }


# ============================================================
# 6.7.1 跟 mls.db 對接 (跟「盤後資金健康度」共用主資料庫)
# ============================================================
def write_watchlist_to_mls_db(
    watchlist: Dict[str, dict],
    trade_date: Optional[str] = None,
    reason_prefix: str = "TSS_v1",
) -> int:
    """
    把 TSS 篩選結果寫入 mls.db 的 watchlist 表 (跟「盤後資金健康度」共用)。
    Schema (從 db.py L44-50):
      trade_date / stock_id / stock_name / sector / reason
    共用欄位即可,reverified/demoted/hit 由主排程自己維護。

    sector 從 SECTOR_MAP (config.py) 查;查不到就空字串。
    回傳寫入筆數。
    """
    import sqlite3
    if not MLS_DB_PATH.exists():
        print(f"[tss] 找不到 {MLS_DB_PATH.name},跳過寫入")
        return 0

    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")

    # 讀 SECTOR_MAP / NAME_MAP 從 config.py
    cfg_path = MONEY_HEALTH_DIR / "config.py"
    sector_map = {}
    name_map = {}
    if cfg_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("mls_money_health_config_2", cfg_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sector_map = getattr(mod, "SECTOR_MAP", {})
            name_map = getattr(mod, "NAME_MAP", {})
        except Exception:
            pass

    conn = sqlite3.connect(str(MLS_DB_PATH))
    cur = conn.cursor()
    written = 0
    try:
        for code, r in watchlist.items():
            if not r.get("final_signal"):
                continue
            sec = sector_map.get(code, ("", ""))[0]
            name = name_map.get(code, "")
            reason = (
                f"{reason_prefix}|bs={r.get('bs_ratio_daily', 0):.2f}|"
                f"close={r.get('close', 0)}|bias={r.get('bias_pct', 0)}%"
            )
            cur.execute(
                """
                INSERT OR REPLACE INTO watchlist
                (trade_date, stock_id, stock_name, sector, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (trade_date, code, name, sec, reason),
            )
            written += 1
        conn.commit()
    except Exception as e:
        print(f"[tss] 寫 watchlist 失敗: {e}")
        conn.rollback()
    finally:
        conn.close()
    return written


def read_watchlist_from_mls_db(trade_date: Optional[str] = None) -> pd.DataFrame:
    """
    從 mls.db 讀 watchlist (盤中排程要拿別人寫的清單時用)。
    """
    import sqlite3
    if not MLS_DB_PATH.exists():
        return pd.DataFrame()
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(MLS_DB_PATH))
    try:
        df = pd.read_sql_query(
            "SELECT * FROM watchlist WHERE trade_date = ? ORDER BY stock_id",
            conn,
            params=(trade_date,),
        )
        return df
    except Exception as e:
        print(f"[tss] 讀 watchlist 失敗: {e}")
        return pd.DataFrame()
    finally:
        conn.close()


# ============================================================
# 6.8 盤中觸發模組 (規格書 第五章)
# ============================================================
@dataclass
class IntradayConfig:
    """盤中觸發四條件閾值 + 強制停止條件閾值"""
    # 條件 1: 大盤穩定性
    market_require_above_open: bool = True

    # 條件 2: 價格突破
    breakout_window_min: int = 30  # 突破前 30 分鐘高點
    breakout_buffer_pct: float = 0.0  # 突破容忍度 (%)

    # 條件 3: 量能增溫
    vol_ratio_threshold: float = 1.2  # 預估量 > 昨日量 × 1.2
    out_vol_ratio_min: float = 0.55  # 外盤比 > 0.55

    # 條件 4: 主動買賣盤 (規格書 5. 表)
    relaxed_cum_ratio: float = 1.1  # 開盤 30 分鐘寬鬆
    relaxed_recent_ratio: float = 1.2
    strict_cum_ratio: float = 1.2  # 盤中嚴苛
    strict_recent_ratio: float = 1.5

    # 強制停止條件 (規格書 第六章)
    force_stop_index_drop_pct: float = 1.5  # 大盤跌破開盤 -1.5%
    force_stop_5min_vol_ratio: float = 10.0  # 5 分 K 量 > 昨日總量 × 10%
    earnings_blackout_days: int = 7


INTRADAY_CFG = IntradayConfig()


@dataclass
class IntradayDecision:
    """盤中每次 tick 評估後的決策"""
    code: str
    ts: datetime
    signal: bool
    conditions_pass: Dict[str, bool]
    force_stop: bool
    force_stop_reason: Optional[str]
    bs_ratio_full: float
    bs_ratio_5min: float
    last_price: float
    action: str  # "buy" / "wait" / "force_stop"


def filter_intraday(
    code: str,
    tracker: ShioajiActiveVolumeTracker,
    index_open: float,
    index_last: float,
    stock_open: float,
    stock_prev_high: float,
    prev_day_volume: int,
    est_today_volume: int,
    breakout_high: float,
    last_5min_volume: int,
    recent_outside_vol: int,
    recent_total_vol: int,
    cum_outside_vol: int,
    cum_total_vol: int,
    in_force_stop_window: bool = False,
    earnings_blackout: bool = False,
) -> IntradayDecision:
    """
    盤中觸發評估 (規格書 第五章)。

    輸入:
      - tracker: 已經被餵 ticks 的 ShioajiActiveVolumeTracker
      - index_open / index_last: 大盤今日開盤 / 最新價
      - stock_open: 個股今日開盤
      - stock_prev_high: 昨日最高 (條件 2 預備,但實際是 breakout_high)
      - breakout_high: 前 30 分鐘最高 (條件 2 用)
      - prev_day_volume: 昨日總量 (條件 3)
      - est_today_volume: 今日預估量 (條件 3,簡化用 current total vol)
      - last_5min_volume: 近 5 分鐘總量 (條件 6 強制停止用)
      - recent_outside_vol / recent_total_vol: 近 5 分鐘外盤 / 總量 (條件 4 recent ratio)
      - cum_outside_vol / cum_total_vol: 全天外盤 / 總量 (條件 4 cum ratio)
      - in_force_stop_window: 是否為開盤 30 分鐘內 (寬鬆 vs 嚴苛切換)

    回傳: IntradayDecision
    """
    snap = tracker.snapshot()

    # --- 條件 1: 大盤穩定性 (現價 > 開盤) ---
    cond1 = index_last > index_open

    # --- 條件 2: 價格突破 (現價 > 前 30 分鐘最高) ---
    last_price = snap.last_tick_price
    cond2 = last_price > breakout_high * (1 + INTRADAY_CFG.breakout_buffer_pct / 100)

    # --- 條件 3: 量能增溫 ---
    cond3_vol = est_today_volume > prev_day_volume * INTRADAY_CFG.vol_ratio_threshold
    out_ratio = (recent_outside_vol / recent_total_vol) if recent_total_vol else 0
    cond3_out = out_ratio > INTRADAY_CFG.out_vol_ratio_min
    cond3 = cond3_vol and cond3_out

    # --- 條件 4: 主動買賣盤 (寬鬆 vs 嚴苛) ---
    cum_ratio = (cum_outside_vol / (cum_total_vol or 1))
    recent_ratio = (recent_outside_vol / (recent_total_vol or 1))

    if in_force_stop_window:
        cond4_cum = cum_ratio > INTRADAY_CFG.relaxed_cum_ratio / (1 + INTRADAY_CFG.relaxed_cum_ratio)
        cond4_recent = recent_ratio > INTRADAY_CFG.relaxed_recent_ratio / (1 + INTRADAY_CFG.relaxed_recent_ratio)
    else:
        cond4_cum = cum_ratio > INTRADAY_CFG.strict_cum_ratio / (1 + INTRADAY_CFG.strict_cum_ratio)
        cond4_recent = recent_ratio > INTRADAY_CFG.strict_recent_ratio / (1 + INTRADAY_CFG.strict_recent_ratio)
    cond4 = cond4_cum and cond4_recent

    # --- 強制停止 (規格書 第六章) ---
    force_stop = False
    force_stop_reason = None
    if earnings_blackout:
        force_stop = True
        force_stop_reason = "earnings_blackout"
    elif index_open and (index_open - index_last) / index_open * 100 > INTRADAY_CFG.force_stop_index_drop_pct:
        force_stop = True
        force_stop_reason = "index_drop"
    elif prev_day_volume and last_5min_volume > prev_day_volume * INTRADAY_CFG.force_stop_5min_vol_ratio / 100:
        force_stop = True
        force_stop_reason = "volume_spike"

    final = cond1 and cond2 and cond3 and cond4 and not force_stop

    if force_stop:
        action = "force_stop"
    elif final:
        action = "buy"
    else:
        action = "wait"

    return IntradayDecision(
        code=code,
        ts=datetime.now(),
        signal=final,
        conditions_pass={"C1_market": cond1, "C2_breakout": cond2, "C3_vol": cond3, "C4_bs": cond4},
        force_stop=force_stop,
        force_stop_reason=force_stop_reason,
        bs_ratio_full=cum_ratio,
        bs_ratio_5min=recent_ratio,
        last_price=last_price,
        action=action,
    )


def run_intraday_loop(
    api,
    watchlist: List[str],
    duration_min: int = 240,
    on_decision=None,
):
    """
    盤中 tick loop 主程式 (規格書 第五章 + 第七章)。
    09:00 ~ 13:30 每分鐘執行觸發判斷,打 watchdog。

    流程:
      1. 訂閱 watchlist 個股的 tick
      2. set_on_tick_stk_v1_callback 把 tick 餵進 tracker
      3. 每分鐘 cron 評估一次,呼叫 on_decision callback

    注: 這是 MVP 雛形。完整版需要:
      - 大盤訂閱 (條件 1)
      - 日 K 抓昨日量 (條件 3)
      - 前 30 分鐘高點動態計算 (條件 2)
      - 開盤 30 分鐘寬鬆 → 嚴苛切換 (條件 4)
      - 風控: 停損停利 (規格書 第八章)
    """
    import shioaji as sj

    trackers: Dict[str, ShioajiActiveVolumeTracker] = {code: ShioajiActiveVolumeTracker(code) for code in watchlist}

    def _on_tick(exchange, tick):
        code = getattr(tick, "code", "")
        if code in trackers:
            trackers[code].add_tick(tick)
            # 簡化: 收到 tick 就印
            snap = trackers[code].snapshot()
            print(f"  [{tick.datetime}] {code} price={snap.last_tick_price} "
                  f"buy={snap.total_buy_vol} sell={snap.total_sell_vol} "
                  f"5min={snap.bs_ratio_5min:.2%}")

    api.set_on_tick_stk_v1_callback(_on_tick)

    for code in watchlist:
        contract = api.Contracts.Stocks.TSE[code]
        api.subscribe(contract)
        print(f"  訂閱 {code}")

    print(f"⏰ 盤中 tick loop 啟動 ({duration_min} 分鐘)")
    print(f"   監控清單: {watchlist}")
    print(f"   按 Ctrl+C 中止")
    print()
    try:
        end_time = datetime.now() + timedelta(minutes=duration_min)
        while datetime.now() < end_time:
            time.sleep(60)
            # 每分鐘評估一次 (簡化版,實際需要帶入 index_open / breakout_high 等)
            for code, tr in trackers.items():
                snap = tr.snapshot()
                if on_decision:
                    on_decision(code, snap)
    except KeyboardInterrupt:
        print("\n🛑 中止 tick loop")
    finally:
        for code in watchlist:
            try:
                api.unsubscribe(api.Contracts.Stocks.TSE[code])
            except Exception:
                pass


# ============================================================
# 7. Shioaji 登入小工具 (規格書 9. 憑證登入)
# ============================================================
def shioaji_login(api_key: Optional[str] = None, secret_key: Optional[str] = None):
    """
    從 env 讀 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY (對齊 Vercel 命名)。
    純回測用,不下單,可不帶 ca_path。
    """
    import shioaji as sj

    api_key = api_key or os.environ.get("SHIOAJI_API_KEY")
    secret_key = secret_key or os.environ.get("SHIOAJI_SECRET_KEY")

    if not api_key or not secret_key:
        raise RuntimeError(
            "缺少 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY。"
            "請先 export,或放 ~/.credentials/shioaji.env 後 source。"
        )

    api = sj.Shioaji()
    api.login(api_key=api_key, secret_key=secret_key)
    return api


# ============================================================
# 8. CLI entry
# ============================================================
def main():
    import argparse
    p = argparse.ArgumentParser(description="TSS v1.0 MVP — 四因子盤後篩選")
    p.add_argument("--code", default="2330", help="個股代號 (default 2330)")
    p.add_argument("--days", type=int, default=30, help="回測天數")
    p.add_argument(
        "--dry-run", action="store_true",
        help="用 mock 資料跑,不登入券商",
    )
    args = p.parse_args()

    print(f"🚀 TSS v1.0 MVP 啟動")
    print(f"   標的: {args.code} | 回測天數: {args.days} 天 | 模式: {'DRY-RUN' if args.dry_run else 'LIVE'}")

    if args.dry_run:
        print("📦 生 mock 1 分 K...")
        stock_1m = generate_mock_1m_kbars(days=args.days)
    else:
        api = shioaji_login()
        contract = api.Contracts.Stocks.TSE[args.code]
        end = datetime.now()
        start = end - timedelta(days=args.days)
        stock_1m = fetch_1min_kbars(api, contract, start, end)
        api.logout()

    print(f"📥 取得 {len(stock_1m)} 根 1 分 K")

    # 計算 Buy/Sell Vol
    stock_1m = classify_buy_sell_vol(stock_1m)
    print(f"💰 Buy Vol 合計: {stock_1m['Buy_Vol'].sum():,} | Sell Vol 合計: {stock_1m['Sell_Vol'].sum():,}")

    # 跑四因子篩選 (沒大盤/籌碼資料所以 C1/C2 跳過)
    result = filter_after_market(stock_1m)

    print("\n" + "=" * 50)
    print("📊 TSS v1.0 篩選結果")
    print("=" * 50)
    print(f"   進場日: {result.get('trade_date')}")
    print(f"   收盤價: {result.get('close')}")
    print(f"   MA20:   {result.get('ma20')}")
    print(f"   乖離率: {result.get('bias_pct')}%")
    print(f"   Buy Vol: {result.get('buy_vol'):,}")
    print(f"   Sell Vol: {result.get('sell_vol'):,}")
    print(f"   BS Ratio (日): {result.get('bs_ratio_daily')}")
    print()
    for k, v in result.get("conditions", {}).items():
        print(f"   {k}: {v}")
    print()
    print(f"🎯 Final Signal: {result.get('final_signal')}")
    if result.get("force_stop"):
        print(f"🛑 Force Stop: {result.get('force_stop_reason')}")
    print()
    print("✅ 跑完。可接 main.py 或排程。")


if __name__ == "__main__":
    main()