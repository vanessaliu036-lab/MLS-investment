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

EARLY, ARMED, TRIGGER, EXTENDED, WATCH = "EARLY", "ARMED", "TRIGGER", "EXTENDED", "—"

STAGE_NOTE = {
    EARLY: "資金先到、價格未動",
    ARMED: "資金＋量能開始同步",
    TRIGGER: "接近啟動,可進盤中確認",
    EXTENDED: "已漲太多,不追",
    WATCH: "條件未成形",
}


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def describe(close, ma5, volume, vol_ma20, foreign_days,
             prev_high=None, high5=None) -> dict:
    """回傳四個判斷依據 + 階段。任何缺值都標 None,不猜。"""
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
    price_state = ("延伸" if (bo5 is not None and bo5 >= BREAKOUT_EXTENDED)
                   else "啟動" if broke_yh
                   else "整理" if broke_yh is not None else None)

    # 階段:先擋過熱,再由「資金 → 量能 → 價格」依序升階
    hot = (ma5_state == "過熱") or (price_state == "延伸") or (vol_state == "爆量")
    strong = foreign == "轉強"
    if hot:
        stage = EXTENDED
    elif strong and vol_state == "抬升" and price_state == "啟動":
        stage = TRIGGER
    elif strong and vol_state == "抬升":
        stage = ARMED
    elif strong and vol_state == "未啟動":
        stage = EARLY
    else:
        stage = WATCH

    nxt = {EARLY: "等待量能抬升 → ARMED", ARMED: "等待突破昨高 → TRIGGER",
           TRIGGER: "盤中確認後可進場", EXTENDED: "不追,等回檔重整",
           WATCH: "等待外資轉強"}[stage]
    return {
        "stage": stage, "stage_note": STAGE_NOTE[stage], "next_step": nxt,
        "foreign_state": foreign, "foreign_days": fd,
        "volume_state": vol_state, "volume_ratio": (round(vr, 2) if vr else None),
        "ma5_state": ma5_state, "ma5_distance_pct": (round(dist * 100, 2) if dist is not None else None),
        "price_state": price_state,
        "breakout_5d_pct": (round(bo5 * 100, 2) if bo5 is not None else None),
        "do_not_chase": stage == EXTENDED,
        "rule_version": "pre_activation_rules_v1_2026-08-24",
    }
