"""Pre-Activation 規則版階段判定(2026-08-24 上線)。

⚠ 這是**規則版**,不是模型。四個階段完全由已驗證的欄位直接判,
不輸出任何未經驗證的分數(Activation Score / Tradeability Score /
Entry Confidence 一律不給)—— 沒有模型依據的分數比沒有分數更危險。

判定依據只用回測站得住的東西:
  · 外資連買(foreign_days,非三法人合計):51 檔母體 train +0.350% /
    test +0.484% 的 T+3 超額,跨時間站得住
  · 量能(volume / vol_ma20):Reverse Winner Mining 在 discovery 與 verify
    都 replicate,T-3 起明顯抬升(效果量 +0.086 → +0.119)
  · 距 MA5:F1 顯示 distance_to_ma5 對未來 T+3 是**負向**且單調(-1.0),
    越遠離越容易回吐 —— 所以它在這裡是「禁追」條件,不是加分項

⚠ 上面三條是**個別因子**的回測證據,不是「三者 AND 起來的 TRIGGER 判定」
本身的證據 —— 這是兩件事。2026-08-24 用原始規則(FOREIGN_STRONG_DAYS=2)
回溯 2024-01~2025-12(51 檔×約485交易日)發現:TRIGGER 完全沒有贏過
「這 51 檔任何一天隨便買」的基準線(T+7 淨命中率 45.7% vs 基準線
46.3%),分 bull/range/bear 三種 regime 重算也一樣。調緊 foreign_days
門檻(discovery/confirm 雙期掃描)找到 fd>=4 可複現 +邊際,但**使用者
2026-08-24 定案:不再用調門檻的方式救 TRIGGER 當進場訊號**——樣本量
(discovery n=95)相對於「换一個規則就能過關」的可能性太薄,屬於調參
風險。詳見專案記憶 pa-trigger-no-edge-vs-baseline。

**TRIGGER 現況定位:只是分析用的階段標籤,不是進場訊號**(下方 next_step
文案已對應調整)。真正回答「哪些特徵組合能提前抓到未來高報酬」的工作,
换成連續特徵 + 回歸目標的 Pre-Activation High-Payoff v1,
見 winning_model_backtest/pa_high_payoff/。

主排序仍由 Legacy(continuation_score)負責;本檔只提供階段與判斷依據。
"""
from __future__ import annotations
from typing import Optional

# 門檻(事前寫死,上線後要改須留紀錄)
FOREIGN_STRONG_DAYS = 2        # 外資連買天數 >= 此值視為轉強
FOREIGN_VERY_STRONG = 5        # 回測驗證過的門檻
VOL_RISING = 1.2               # 量比 >= 此值視為抬升
VOL_BLOWOFF = 2.5              # 量比 >= 此值視為爆量
MA5_NEAR = 0.02                # 距 MA5 <= 2% 視為貼近
MA5_HOT = 0.07                 # 距 MA5 >= 7% 視為過熱(與引擎 HIGH_BIAS_PCT 一致)
BREAKOUT_EXTENDED = 0.03       # 高過前波 5 日高 3% 以上視為延伸

EARLY, ARMED, TRIGGER, EXTENDED, WATCH, ACTIVE = \
    "EARLY", "ARMED", "TRIGGER", "EXTENDED", "—", "ACTIVE"

STAGE_NOTE = {
    EARLY: "資金先到、價格未動",
    ARMED: "資金＋量能開始同步",
    TRIGGER: "啟動條件成形,無進場證據,僅供觀察",
    EXTENDED: "已漲太多,不追",
    WATCH: "條件未成形",
    ACTIVE: "價格已啟動(含漲停),量能尚未跟上",
}


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def describe(close, ma5, volume, vol_ma20, foreign_days,
             prev_high=None, high5=None, is_limit_up=None) -> dict:
    """回傳四個判斷依據 + 階段。任何缺值都標 None,不猜。

    is_limit_up:當日是否觸及漲停。價格 Activation 與量能 Confirmation 是
    兩個獨立訊號 —— 曾發生「已經漲停,量能未達門檻」被印成「量能：未啟動,
    等待量能抬升 → ARMED」,讓使用者誤讀成股票還沒動。價格側一旦確認啟動
    (漲停或 close > prev_high),就不可再落入 EARLY/WATCH 這種「價格未動」
    的敘述,改用獨立的 ACTIVE 階段。"""
    c, m5 = _num(close), _num(ma5)
    v, vm = _num(volume), _num(vol_ma20)
    fd = _num(foreign_days)

    dist = (c / m5 - 1) if (c is not None and m5) else None
    vr = (v / vm) if (v is not None and vm) else None
    bo5 = (c / _num(high5) - 1) if (c is not None and _num(high5)) else None
    broke_yh = (c > _num(prev_high)) if (c is not None and _num(prev_high)) else None

    foreign = ("轉強" if (fd is not None and fd >= FOREIGN_STRONG_DAYS)
               else "轉弱" if (fd is not None and fd <= -FOREIGN_STRONG_DAYS)
               else "中性" if fd is not None else None)
    vol_state = ("爆量" if (vr is not None and vr >= VOL_BLOWOFF)
                 else "抬升" if (vr is not None and vr >= VOL_RISING)
                 else "未啟動" if vr is not None else None)
    ma5_state = ("過熱" if (dist is not None and dist >= MA5_HOT)
                 else "貼近" if (dist is not None and abs(dist) <= MA5_NEAR)
                 else "正常" if dist is not None else None)
    price_state = ("漲停" if is_limit_up
                   else "延伸" if (bo5 is not None and bo5 >= BREAKOUT_EXTENDED)
                   else "啟動" if broke_yh
                   else "整理" if broke_yh is not None else None)
    price_activated = price_state in ("漲停", "延伸", "啟動")

    # 階段:先擋過熱,再由「資金 → 量能 → 價格」依序升階。
    # 價格側一旦確認啟動(含漲停),就先攔在 ACTIVE,不准掉回 EARLY/WATCH
    # 那種「價格未動」的敘述 —— 這是本次修正的重點,價格 Activation 與
    # 量能 Confirmation 是兩個獨立訊號,不能被單一 stage 蓋掉。
    hot = (ma5_state == "過熱") or (price_state == "延伸") or (vol_state == "爆量")
    strong = foreign == "轉強"
    if hot:
        stage = EXTENDED
    elif price_activated and vol_state != "抬升":
        stage = ACTIVE
    elif strong and vol_state == "抬升" and price_state in ("啟動", "漲停"):
        stage = TRIGGER
    elif strong and vol_state == "抬升":
        stage = ARMED
    elif strong and vol_state == "未啟動":
        stage = EARLY
    else:
        stage = WATCH

    # TRIGGER 的 next_step 曾寫「盤中確認後可進場」——2026-08-24 大樣本回測
    # (n=555)證明 TRIGGER 對 51 檔基準線沒有 entry edge,不得再暗示可進場。
    # TRIGGER 現在只是「啟動條件成形」的分析標籤,不是訊號。
    nxt = {EARLY: "等待量能抬升 → ARMED", ARMED: "等待突破昨高 → TRIGGER",
           TRIGGER: "啟動條件成形,持續觀察", EXTENDED: "不追,等回檔重整",
           WATCH: "等待外資轉強",
           ACTIVE: "不追,觀察是否鎖停／隔日承接"}[stage]
    return {
        "stage": stage, "stage_note": STAGE_NOTE[stage], "next_step": nxt,
        "foreign_state": foreign, "foreign_days": fd,
        "volume_state": vol_state, "volume_ratio": (round(vr, 2) if vr else None),
        "ma5_state": ma5_state, "ma5_distance_pct": (round(dist * 100, 2) if dist is not None else None),
        "price_state": price_state, "price_activated": price_activated,
        "breakout_5d_pct": (round(bo5 * 100, 2) if bo5 is not None else None),
        "do_not_chase": stage in (EXTENDED, ACTIVE),
        "rule_version": "pre_activation_rules_v1_2026-08-27",
    }
