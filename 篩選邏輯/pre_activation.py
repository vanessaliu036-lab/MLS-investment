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
PRICE_ACTIVATED_CHANGE = 9.5   # 漲幅(%) >= 此值視為價格已啟動,即使當下未技術鎖死漲停
                                # (2026-08-27 使用者要求:盤中早段鎖漲停/跳動可能讓
                                # is_limit_up 判定有時差,漲幅門檻當第二道保險)

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
             prev_high=None, high5=None, is_limit_up=None, change_rate=None) -> dict:
    """回傳四個判斷依據 + 階段。任何缺值都標 None,不猜。

    is_limit_up / change_rate:價格 Activation 與量能 Confirmation 是兩個
    獨立訊號 —— 曾發生「已經漲停,量能未達門檻」被印成「量能：未啟動,
    等待量能抬升 → ARMED」,讓使用者誤讀成股票還沒動。只要「盤中觸及漲停
    (is_limit_up)」或「漲幅 >= PRICE_ACTIVATED_CHANGE(9.5%)」任一成立,
    價格側就視為已啟動,直接 override,不可再落入 EARLY/WATCH 這種
    「價格未動」的敘述,改用獨立的 ACTIVE 階段。"""
    c, m5 = _num(close), _num(ma5)
    v, vm = _num(volume), _num(vol_ma20)
    fd = _num(foreign_days)
    chg = _num(change_rate)

    dist = (c / m5 - 1) if (c is not None and m5) else None
    vr = (v / vm) if (v is not None and vm) else None
    bo5 = (c / _num(high5) - 1) if (c is not None and _num(high5)) else None
    broke_yh = (c > _num(prev_high)) if (c is not None and _num(prev_high)) else None
    near_limit_up = (chg is not None and chg >= PRICE_ACTIVATED_CHANGE)

    foreign = ("轉強" if (fd is not None and fd >= FOREIGN_STRONG_DAYS)
               else "轉弱" if (fd is not None and fd <= -FOREIGN_STRONG_DAYS)
               else "中性" if fd is not None else None)
    vol_state = ("爆量" if (vr is not None and vr >= VOL_BLOWOFF)
                 else "抬升" if (vr is not None and vr >= VOL_RISING)
                 else "未啟動" if vr is not None else None)
    ma5_state = ("過熱" if (dist is not None and dist >= MA5_HOT)
                 else "貼近" if (dist is not None and abs(dist) <= MA5_NEAR)
                 else "正常" if dist is not None else None)
    price_state = ("漲停" if (is_limit_up or near_limit_up)
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
        "volume_confirmed": vol_state in ("抬升", "爆量") if vol_state is not None else None,
        "volume_confirmation_state": (
            "已確認" if vol_state in ("抬升", "爆量") else
            "尚未確認" if vol_state == "未啟動" else None),
        "ma5_state": ma5_state, "ma5_distance_pct": (round(dist * 100, 2) if dist is not None else None),
        "price_state": price_state, "price_activated": price_activated,
        "breakout_5d_pct": (round(bo5 * 100, 2) if bo5 is not None else None),
        "do_not_chase": stage in (EXTENDED, ACTIVE),
        "rule_version": "pre_activation_rules_v1_2026-08-27",
    }


def overlay_live_price_activation(result: dict, *, is_limit_up=None,
                                   change_rate=None) -> dict:
    """把盤中價格啟動疊回盤後 PA 快照,但不改寫量能確認。

    ``candidate_pool`` 的 PA 是盤後快照,盤中若直接照貼,會出現「現在已漲停
    但仍顯示 EARLY／等待 ARMED」的時間順序錯置。這個純函式只處理價格側
    override; volume_state 仍保留原值,讓價格 Activation 與 Volume Confirmation
    維持兩個獨立訊號。

    EXTENDED 保留原狀態,因為那是更高優先級的禁追風險；若量能已是「抬升」,
    也保留原本的 ARMED/TRIGGER 語意。只有價格已啟動且量能尚未確認時,升成
    ACTIVE。
    """
    out = dict(result or {})
    try:
        chg = float(change_rate) if change_rate is not None else None
    except (TypeError, ValueError):
        chg = None
    price_active = bool(is_limit_up) or (
        chg is not None and chg >= PRICE_ACTIVATED_CHANGE)
    if not price_active:
        return out
    if out.get("stage") == EXTENDED or out.get("volume_state") in ("抬升", "爆量"):
        return out

    out.update({
        "stage": ACTIVE,
        "stage_note": STAGE_NOTE[ACTIVE],
        "next_step": "不追,觀察是否鎖停／隔日承接",
        "price_state": "漲停",
        "price_activated": True,
        "volume_confirmed": out.get("volume_state") in ("抬升", "爆量"),
        "volume_confirmation_state": (
            "已確認" if out.get("volume_state") in ("抬升", "爆量") else
            "尚未確認" if out.get("volume_state") == "未啟動" else None),
        "do_not_chase": True,
        "price_activation_source": "盤中漲停／漲幅≥9.5%",
    })
    return out


def overlay_foreign_confirmation(result: dict, chip: dict) -> dict:
    """把最新已完成交易日的外資快取疊回 PA，不改價格/量能階段。

    外資是盤後日資料，盤中只讀快取；這個 overlay 只更新「外資判讀」
    與資料來源/資料日，避免 candidate_pool 早於籌碼快取建立時留下
    ``外資：—``。價格 Activation 與 Volume Confirmation 仍由各自規則決定。
    """
    out = dict(result or {})
    chip = chip or {}
    streak = chip.get("foreign_days", chip.get("inst_streak"))
    if streak is None:
        return out
    try:
        streak = float(streak)
    except (TypeError, ValueError):
        return out
    if streak >= FOREIGN_STRONG_DAYS:
        state = "轉強"
    elif streak <= -FOREIGN_STRONG_DAYS:
        state = "轉弱"
    else:
        state = "中性"
    out.update({
        "foreign_days": streak,
        "foreign_state": state,
        "foreign_net_d": chip.get("foreign_net_d"),
        "foreign_net_3d": chip.get("foreign_net_3d"),
        "foreign_net_5d": chip.get("foreign_net_5d"),
        "foreign_net_20d": chip.get("foreign_net_20d"),
        "foreign_source": chip.get("source"),
        "foreign_source_date": chip.get("source_date"),
    })
    return out
