"""
intraday_verdict.py — AI 盤中判讀(後台算結論,前台只印)

2026-08-18 二次改版(核心邏輯改版,不只是換字句):
舊版是「條件 A+B+C → 套一段預先寫好的模板」,20 檔看下來字句幾乎相同
(都在講「同步轉強、關鍵價突破、量能確認、明確啟動」),資訊差異被模板吃掉。

現在改成「先分類、再各講各的」:
  1. dominant_signal — 這檔現在最異常、最值得注意的單一現象(只能選一個,
     不是把五個指標都講一遍)。分類表見 _classify()。
  2. action_bias      — 追／等／看回測／不追／防守／放棄,交易員的態度,
     不是另一個分析指標。
  3. headline + body   — headline 是 dominant_signal 的白話講法(如「量價資金
     共振」「爆量不漲」),body 最多兩句、35~60 個中文字,交易員口吻:
     結論 + 為什麼 + 現在怎麼做。不是「把指標翻譯成中文」的說明文。

**不重述原則**:body 不重述現價、漲跌幅、關鍵價這些 UI 上半部已經顯示的數字;
但當某個數字就是「為什麼」本身(例如量能只有 0.2x 撐不起漲幅),直接寫出來
比迂迴形容更清楚,允許引用那一個數字 —— 只引用支撐這句判斷的那一個,不是
全部欄位都念一遍。

next_steps 維持:接下來什麼變化會升級/降級,帶實際價位,使用者照著操作。

鐵律(與 [說明語意層] 一致):只翻譯既有事實,不參與篩選、不改變去留。
缺資料就說沒有,不猜 —— 缺價或缺關鍵價位時回 pending,不寫「維持強勢」。
"""
from __future__ import annotations


def _f(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _n(x) -> str:
    """價位文字:整數不帶小數點,零頭保留 —— 169 而不是 169.0。"""
    v = _f(x)
    if v is None:
        return "—"
    return str(int(v)) if abs(v - int(v)) < 1e-9 else f"{v:g}"


# 量能倍率分界(2026-08-18 修「同模板不同數字」bug 加入)。同樣是「價格站上關鍵價
# ＋資金流入」,量能 0.2x 和 1.2x 是完全不同等級的啟動品質,不能套同一句判讀。
# < LOW：量能沒跟上,價格是先行指標,不是完整啟動,容易是誘多。
VOL_CONFIRM_LOW = 0.8

# 距買點乖離分級(2026-08-18 補「訊號好≠現在能追」這層)。「股票很強」和「現在適合
# 追」是兩件事:同樣量價資金共振,現價貼著買點 vs 已經拉開一大截,可交易性完全不同。
# 分級只由距買點決定基礎級距,VWAP/日內位置/量能/資金趨勢是修正項,不是各自獨立打分。
DISTANCE_TIERS = [1.0, 2.0, 4.0]     # 對應 低/中/中高,超過最後一檔=高
RISK_LABELS = ["低", "中", "中高", "高"]


def _distance_tier(distance_pct: float) -> int:
    for i, cap in enumerate(DISTANCE_TIERS):
        if distance_pct <= cap:
            return i
    return len(DISTANCE_TIERS)


def _chase_risk(distance_pct, vwap_diff_pct, day_position_pct, vr, money_trend):
    """回傳 {"tier": 0-3, "label": 低/中/中高/高} 或 None(還沒站上買點,不適用)。"""
    if distance_pct is None or distance_pct < 0:
        return None
    tier = _distance_tier(distance_pct)
    if vwap_diff_pct is not None and vwap_diff_pct < 0:
        tier += 1                              # 已跌破今日均價,多數成本在套牢
    if day_position_pct is not None and day_position_pct < 50:
        tier += 1                              # 已從高點回落,不是站在日內高檔
    if vr is not None and vr < VOL_CONFIRM_LOW:
        tier += 1                              # 量能沒跟上,追價缺乏成交量支撐
    if money_trend == "down":
        tier += 1                              # 資金流入正在收斂,力道轉弱
    tier = min(tier, len(RISK_LABELS) - 1)
    return {"tier": tier, "label": RISK_LABELS[tier]}


def _trade_status(above, touched, flow_in, flow_out, chase_risk, vol_weak):
    """交易狀態:「現在到底怎麼做」的單一結論,不是另一個分析指標。"""
    if above and flow_out:
        return {"code": "🔴", "label": "不進,價量背離"}
    if not above:
        return ({"code": "🟡", "label": "曾觸及未站穩,等重新站上"} if touched else
                {"code": "⚪", "label": "未觸發,續觀察"})
    if chase_risk is None:
        return None
    tier = chase_risk["tier"]
    if tier == 0 and flow_in and not vol_weak:
        return {"code": "🔥", "label": "可進場"}
    if tier <= 1 and flow_in and not vol_weak:
        return {"code": "🟢", "label": "可小倉試單"}
    return {"code": "🟡", "label": "已啟動,不建議追價,等回測"}


BLOWOUT_VOL = 2.0     # 量能到這個倍數還推不動價格,判「爆量不漲」而非普通強勢
BLOWOUT_FLAT_PCT = 1.5


def _classify(*, above, touched, flow_in, flow_out, vr, dist, ch, money_trend):
    """回傳 (signal_key, headline, tone, body, action_bias)。
    優先序由上到下,第一個成立的規則就是結論 —— 不是五個條件疊加成一段長文,
    是從一堆事實裡挑出「現在最該注意的那一件」。"""
    vol_weak = vr is not None and vr < VOL_CONFIRM_LOW
    blowout = vr is not None and vr >= BLOWOUT_VOL and ch is not None and abs(ch) < BLOWOUT_FLAT_PCT

    if above and flow_out:
        return ("price_fund_diverge", "價漲資金流出", "caution",
                "站上關鍵價，主動資金卻同時流出——這不是主力在推，先防高檔調節，不宜追。",
                "防守")
    if not above and flow_in and ch is not None and ch < 0:
        return ("dip_absorption", "價跌資金流入", "neutral",
                "股價在跌，錢卻持續進來，這種比單純上漲更值得盯，留意是不是在吸籌。",
                "觀察")
    if not above and touched:
        if dist is not None and dist > -1.5:
            return ("retest", "突破後回測", "caution",
                    "剛跌破關鍵價不遠，還在回測範圍，看能不能重新站回。", "等")
        return ("breakout_fail", "突破後失守", "weak",
                "站上後又跌破，上方賣壓比想像中重，先放棄追這一次。", "放棄")
    if blowout:
        return ("blowout_no_follow", "爆量不漲", "caution",
                f"量能已經放到 {vr:g}x，價格卻推不太動，比量縮更該提防，先當高檔換手。",
                "防守")
    if above:
        if flow_in:
            if vol_weak:
                return ("price_lead_vol_lag", "價漲但量不足", "caution",
                        f"價格資金都動了，量能只有 {vr:g}x，撐不起這段漲幅，"
                        "先當價格先行，不追。", "等量能")
            if dist is not None and dist >= 2.0:
                return ("confluence_extended", "量價資金共振", "caution",
                        "有量有錢的乾淨突破，但已經拉開買點一段，"
                        "等第一次回踩有沒有人接。", "看回測")
            return ("confluence", "量價資金共振", "strong",
                    "價、量、資金三個都動了，這次啟動品質不差，"
                    "等第一次回踩確認承接。", "追或小倉")
        return ("vol_shrink_hold", "縮量守價", "neutral",
                "站上關鍵價，但主動資金還沒跟上，結構成立、動能待確認。", "等資金")
    if flow_in:
        return ("early_absorption", "低檔承接", "neutral",
                "還沒到關鍵價，但資金已經悄悄進場，提前留意動向。", "觀察")
    return ("no_signal", "無明顯訊號", "weak",
            "價格、量能、資金都沒有表態，先放著。", "放棄")


def _momentum_note(signal_key, money_trend):
    """第二句(選用):資金趨勢什麼時候值得再補一句,而不是每檔都講。
    只在「趨勢會改變第一句結論的可信度」時才加,避免又變回萬用模板。"""
    if money_trend == "down" and signal_key in ("confluence", "confluence_extended", "vol_shrink_hold"):
        return "資金流入速度在收斂，觀察是否降溫。"
    if money_trend == "up" and signal_key in ("price_lead_vol_lag", "vol_shrink_hold", "early_absorption"):
        return "資金流入還在加速，量能一旦跟上會轉強。"
    return None


def build(*, price=None, trigger=None, change_rate=None, aflow=None,
          intraday_high=None, track=None, vol_ratio=None,
          distance_pct=None, vwap_diff_pct=None, day_position_pct=None,
          money_trend=None) -> dict:
    """把一檔的盤中事實翻成判讀 + 下一步。所有欄位皆可為 None。"""
    p, t = _f(price), _f(trigger)
    ch, fl, hi, vr = _f(change_rate), _f(aflow), _f(intraday_high), _f(vol_ratio)
    dist, vwd, dpos = _f(distance_pct), _f(vwap_diff_pct), _f(day_position_pct)
    lines: list[dict] = []

    if p is None or t is None:
        if t is not None:
            return {"tone": "pending", "chase_risk": None, "trade_status": None,
                    "lines": [{"tone": "pending",
                               "text": "尚未有盤中報價,今日先看開盤能否站上關鍵價,"
                                       "站上才進入啟動觀察。"}],
                    "next_steps": [
                        {"cond": f"開盤站上 {_n(t)}", "then": "進入啟動觀察"},
                        {"cond": f"開盤在 {_n(t)} 下方", "then": "續留觀察,不追"}]}
        return {"tone": "pending", "chase_risk": None, "trade_status": None,
                "lines": [{"tone": "pending", "text": "盤中資料尚未就緒,無法判讀。"}],
                "next_steps": []}

    above = p >= t
    touched = hi is not None and hi >= t
    flow_in = fl is not None and fl > 0
    flow_out = fl is not None and fl < 0
    vol_weak = vr is not None and vr < VOL_CONFIRM_LOW
    chase_risk = _chase_risk(dist, vwd, dpos, vr, money_trend) if above else None
    trade_status = _trade_status(above, touched, flow_in, flow_out, chase_risk, vol_weak)

    hold = {"cond": f"守住 {_n(t)}", "then": "維持強勢"}
    lose = {"cond": f"跌回 {_n(t)} 下方", "then": "突破失敗,降級觀察"}
    retest = {"cond": f"回測 {_n(t)} 附近守住", "then": "回測不破,第二買點"}
    rebreak = {"cond": f"收盤站回 {_n(t)} 之上", "then": "突破重新成立"}
    fail_confirm = {"cond": f"持續在 {_n(t)} 下方", "then": "觸及失敗,降級觀察"}
    breakout_up = {"cond": f"放量站上 {_n(t)}", "then": "升級為啟動觀察"}
    stay_below = {"cond": "續在關鍵價下方整理", "then": "維持觀察,不進場"}
    vol_confirm_step = {"cond": "量能放大至可確認水準", "then": "升級為完整啟動,可依 ATR 紀律進場"}

    signal_key, headline, tone, body, action_bias = _classify(
        above=above, touched=touched, flow_in=flow_in, flow_out=flow_out,
        vr=vr, dist=dist, ch=ch, money_trend=money_trend)

    lines.append({"tone": tone, "text": body})
    second = _momentum_note(signal_key, money_trend)
    if second:
        lines.append({"tone": tone, "text": second})

    next_steps_by_signal = {
        "price_fund_diverge": [hold, {"cond": "資金流持續為負", "then": "視為假突破,降級觀察"}],
        "confluence": [hold, lose],
        "confluence_extended": [hold, lose, retest],
        "price_lead_vol_lag": [vol_confirm_step, hold, lose],
        "blowout_no_follow": [hold, lose],
        "vol_shrink_hold": [hold, lose],
        "retest": [rebreak, fail_confirm],
        "breakout_fail": [rebreak, fail_confirm],
        "dip_absorption": [breakout_up, stay_below],
        "early_absorption": [breakout_up, stay_below],
        "no_signal": [breakout_up, stay_below],
    }

    return {"tone": tone, "lines": lines, "headline": headline,
            "dominant_signal": signal_key, "action_bias": action_bias,
            "chase_risk": chase_risk, "trade_status": trade_status,
            "next_steps": next_steps_by_signal.get(signal_key, [hold, lose])}


def attach(items) -> int:
    """就地把判讀併進每一檔(唯讀衍生,不改任何既有欄位)。回傳成功筆數。"""
    n = 0
    for it in items:
        try:
            it["intraday_verdict"] = build(
                price=it.get("price") or it.get("close"),
                trigger=it.get("trigger_price") or it.get("entry_ref"),
                change_rate=it.get("change_rate"),
                aflow=it.get("aflow") or it.get("net_active"),
                intraday_high=it.get("intraday_high"),
                track=it.get("track"),
                vol_ratio=it.get("volume_ratio"),
                distance_pct=it.get("chase_distance_pct"),
                vwap_diff_pct=it.get("vwap_diff_pct"),
                day_position_pct=it.get("day_position_pct"),
                money_trend=it.get("money_trend"))
            n += 1
        except Exception:
            it["intraday_verdict"] = None
    return n
