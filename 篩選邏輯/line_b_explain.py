"""line_b_explain.py — Line B 說明語意層(單一嘴巴,仿 explain.py 慣例)。

定位:把 `line_b_watch_ledger` 已經算好的結構化事實,翻成一句白話。
  · 不做判斷:C1/C2/flow_class 早就在後台算好凍結了,這裡只負責「怎麼講」。
  · 不吃原始公式當顯示內容:前台永遠只看「現在多少→要過多少→還差多少→
    資金有沒有來→狀態」,不看 inst_5d 原始數字、不看 C1/C2 布林值本身。
  · 全站唯一嘴巴:任何 Line B 頁面要顯示一句話,一律呼叫這裡,不得各自現算。

輸出合約:
    { "resistance": float, "current": float, "distance_pct": float,
      "chip_summary": "一句白話", "flow_display": "一句白話+方向", "flow_stale": bool,
      "status": "WAIT"|"WATCH_CLOSELY"|"CONFIRMED"|"GIVE_UP",
      "status_label": "現在等"|"重點盯"|"已確認"|"放棄",
      "system_sentence": "一句系統結論",
      "activation_prob": float|None  # 校準過的「今天會不會啟動」機率(0~1),
        # 不是「啟動後會不會賺」——後者要等 forward MFE/MAE 累積才做,
        # 兩者不得混算/混顯示(2026-08-26 Vanessa 明確區分)。已啟動
        # (status=CONFIRMED)或 distance_pct 缺值時回傳 None。
      "calibration_bucket": str|None,   # activation_prob 用的是哪一格,audit log 用
      "calibration_version": str|None,  # CALIBRATION_VERSION,audit log 用
      "confirmed_so_far": bool,         # point-in-time 資金確認狀態,audit log 用
    }

status 判定(唯一規則來源,不得在別處重算):
    CONFIRMED     : watch_mode_activated 為真(A-flow/Watch Mode 已確認;價格是否站上另行呈現)
    WATCH_CLOSELY : 未啟動,但現價距壓力 <= WATCH_DISTANCE_PCT 且今日資金
                    已出現過確認(OPEN_POSITIVE 或 FLOW_FLIP)
    GIVE_UP       : 這一列是「已收盤」的完整一天(is_eod=True),整天 NO_FLIP
                    且未啟動 —— 只在收盤後才判定放棄,盤中進行式不下這個結論
    WAIT          : 其餘情況(包含盤中仍在觀察、資金尚未確認、距離仍遠)
"""
from __future__ import annotations
from typing import Optional

WATCH_DISTANCE_PCT = 1.5  # 現價距壓力 <=1.5% 且資金已確認 → 重點盯(唯一調整過的呈現門檻,非研究門檻)

# ── 啟動機率校準表(2026-08-26 重跑校準,對 /opt/mls-screen/mls.db 產本資料直接
# 重算,非現算非編造;腳本邏輯與 run_line_b_ledger.py 的 _c1_c2/_flow_and_activation
# 完全對齊)。target = P(這個 stock-day 當天最終進入 WATCH MODE | 當下狀態(距壓力%,
# 資金是否已出現過確認)),樣本 = C1+C2 通過名單、11個乾淨交易日(2026-08-11~08-25,
# 排除 08-04 暖機不足 + 08-05/06/07/10 A-flow 卡死事故 + 08-26 當天未收盤)、
# 77 個 stock-day、1710 個 pre-activation snapshot。這只是「會不會啟動」,不是
# 「啟動後會不會賺」——後者要等 forward MFE/MAE 累積才能做,不得混算(見 Vanessa
# 2026-08-26 的明確區分)。
#
# ⚠ Causal 完整性(2026-08-26 校準時明確要求):「資金是否已確認」是逐格重算的
# point-in-time 狀態(只用該格與之前的 net_active 判斷 OPEN_POSITIVE/FLOW_FLIP),
# 不是拿收盤後才知道的整天 flow_class 回頭貼標。已站上壓力(distance_pct>=0)或已
# 觸發 WATCH MODE 之後的格子不計入(那不是「會不會啟動」的問題了)。
#
# ⚠ 有效樣本數是 stock-day/day 層級(77 / 11),不是 1710。1710 個 snapshot
# 來自同一批 77 個 stock-day,同一天同一檔股票的多個 snapshot 高度相關——後台驗證
# 信賴區間要以 stock-day/day clustering 算,不能拿 snapshot 數量當獨立樣本數
# (2026-08-26 Vanessa 明確要求)。
#
# ⚠ 各格不強行做成單調:「-1.5~-0.5%」這格「資金已確認」反而比「未確認」低
# (13.9% vs 20.0%)——這是真實資料的雜訊,保留原樣,不修飾成看起來更合理。
_CALIB_BINS = [-100, -6, -3, -1.5, -0.5, 0]
_CALIB_LABELS = ["<-6%", "-6~-3%", "-3~-1.5%", "-1.5~-0.5%", "-0.5~0%"]
_CALIB_TABLE = {
    ("<-6%", False): 0.009, ("<-6%", True): 0.000,
    ("-6~-3%", False): 0.013, ("-6~-3%", True): 0.179,
    ("-3~-1.5%", False): 0.074, ("-3~-1.5%", True): 0.142,
    ("-1.5~-0.5%", False): 0.200, ("-1.5~-0.5%", True): 0.139,
    ("-0.5~0%", False): 0.345, ("-0.5~0%", True): 0.640,
}

# 這張表的版本號——append-only audit log(line_b_audit_log.py)每筆都要記,
# 之後要重建「當時頁面到底顯示多少機率」就靠這個 + bucket + confirmed_so_far。
# 表格內容改變(重新校準/加天數)一定要 bump,不得就地覆蓋舊版本號。
CALIBRATION_VERSION = "lineb_calib_v2_2026-08-26_n77d11_snap1710_pit"


def bucket_label(distance_pct: Optional[float]) -> Optional[str]:
    """回傳 distance_pct 落入的校準格 label,供 audit log 記錄用。distance_pct>=0
    或缺值回傳 None(不適用校準表)。"""
    if distance_pct is None or distance_pct >= 0:
        return None
    for lo, hi, label in zip(_CALIB_BINS[:-1], _CALIB_BINS[1:], _CALIB_LABELS):
        if lo < distance_pct <= hi:
            return label
    return "<-6%"


def activation_probability(distance_pct: Optional[float], confirmed_so_far: bool) -> Optional[float]:
    """回傳校準過的啟動機率(0~1)。distance_pct>=0(已站上)不適用,回傳 None
    (那個狀態不是「會不會啟動」,是已經啟動了)。"""
    if distance_pct is None or distance_pct >= 0:
        return None
    for lo, hi, label in zip(_CALIB_BINS[:-1], _CALIB_BINS[1:], _CALIB_LABELS):
        if lo < distance_pct <= hi:
            return _CALIB_TABLE.get((label, confirmed_so_far))
    return _CALIB_TABLE.get(("<-6%", confirmed_so_far))

STATUS_LABEL = {
    "WAIT": "現在等", "WATCH_CLOSELY": "重點盯",
    "CONFIRMED": "已確認", "GIVE_UP": "放棄",
    "DATA_BLOCKED": "資料阻擋",
}


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _chip_summary(price_5d: Optional[float], close_position: Optional[float],
                  inst_5d: Optional[float]) -> str:
    """C2(賣壓減弱+價格反應)的白話版。只描述方向與強度,不吐原始數字。"""
    if price_5d is None or close_position is None:
        return "籌碼資料不足"
    strong = price_5d >= 5.0 and close_position >= 0.9
    if inst_5d is not None and inst_5d < 0:
        base = "法人賣壓已減弱" if inst_5d > -1500 else "法人賣壓有限"
    else:
        base = "法人偏多"
    tail = "，價格強勢回應" if strong else "，價格開始回應" if price_5d > 0 else "，價格尚未反應"
    return base + tail


def _flow_display(flow_class: Optional[str], magnitude: Optional[float], stale: bool = False) -> str:
    if stale:
        return "資金資料待更新"
    mag_txt = f"{magnitude:+,.0f}" if magnitude is not None else "—"
    arrow = {"OPEN_POSITIVE": "↑", "FLOW_FLIP": "↗", "NO_FLIP": "→"}.get(flow_class, "—")
    word = {"OPEN_POSITIVE": "資金已轉強", "FLOW_FLIP": "資金翻正",
           "NO_FLIP": "資金未轉正"}.get(flow_class, "資金未知")
    return f"{word} {arrow} {mag_txt}"


def explain(row: dict, is_eod: bool = True, flow_stale: bool = False) -> dict:
    """row 需含: t1_close/t1_ma20/t1_prior_high/t1_inst_5d/t1_price_5d/
    t1_close_position/flow_class/flow_confirm_magnitude/watch_mode_activated/
    current_price(現價;沒傳就退回 t_close 或 t1_close)。

    flow_stale(2026-08-26 新增,只有盤中即時頁會傳 True):呼叫端(line_b_live.py)
    已經只用「還新鮮」的 net_active 算出 row 的 flow_class/watch_mode_activated——
    也就是說 row 裡的狀態本身就是凍結在「最後一次確定新鮮」那一刻的正確 point-in-time
    事實,不是拿舊資料硬算出新結論。flow_stale=True 純粹只影響「怎麼講」:资金那一行
    改顯示「資料待更新」,不呈現可能已經過期的方向字/箭頭,但 status/activation_prob
    仍然用(已經凍結、不會再被舊資料污染的)flow_class 算,不整段吃掉——這正是
    「不得沿用舊數字形成新判斷」的意思:不拿舊 tick 生新結論,但已經成立的舊結論
    本身不因為現在没更新而失效。
    """
    resistance = _num(row.get("t1_prior_high"))
    current = _num(row.get("current_price"))
    if current is None:
        current = _num(row.get("t_close")) or _num(row.get("t1_close"))

    distance_pct = None
    if resistance and current is not None:
        distance_pct = round((current / resistance - 1) * 100, 2)

    chip_summary = _chip_summary(_num(row.get("t1_price_5d")), _num(row.get("t1_close_position")),
                                 _num(row.get("t1_inst_5d")))
    flow_class = row.get("flow_class")
    flow_conflict = bool(row.get("flow_conflict") or row.get("aflow_conflict"))
    flow_display = ("資金資料衝突，暫不判定" if flow_conflict else
                    _flow_display(flow_class, _num(row.get("flow_confirm_magnitude")), stale=flow_stale))

    activated = bool(row.get("watch_mode_activated"))
    confirmed_today = flow_class in ("OPEN_POSITIVE", "FLOW_FLIP")

    if flow_conflict:
        status = "DATA_BLOCKED"
    elif activated:
        status = "CONFIRMED"
    elif is_eod and flow_class == "NO_FLIP":
        status = "GIVE_UP"
    elif distance_pct is not None and distance_pct >= -WATCH_DISTANCE_PCT and confirmed_today:
        status = "WATCH_CLOSELY"
    else:
        status = "WAIT"

    bucket = bucket_label(distance_pct)
    activation_prob = (None if activated else
                       activation_probability(distance_pct, confirmed_so_far=confirmed_today))

    if status == "DATA_BLOCKED":
        sentence = "A-flow 來源數值衝突｜暫不判定今日資金方向，等待一致資料"
    elif status == "CONFIRMED":
        # watch_mode_activated 代表 A-flow/Watch Mode 已成立,不等於價格已站上
        # 結構關鍵價。兩者必須分開,否則「現價低於壓力」仍會被說成已站上。
        if distance_pct is not None and distance_pct >= 0:
            # 已站上只證明 Price Trigger；正式 ACTIVE 還必須另有 Volume
            # Quality 與 Acceptance，不能把舊 Watch Mode activation 說成交易啟動。
            sentence = (f"已站上關鍵價 {resistance:,.1f}｜PRICE TRIGGER 已發生，待量能／承接確認"
                        if resistance else "PRICE TRIGGER 已發生，待量能／承接確認")
        elif distance_pct is not None and resistance:
            sentence = f"A-flow 已確認｜尚差關鍵價 {abs(distance_pct):.2f}%（{resistance:,.1f}）"
        else:
            sentence = "A-flow 已確認｜待價格／量能／承接確認"
    elif status == "WATCH_CLOSELY":
        sentence = f"重點盯 {resistance:,.1f}，站穩 {resistance:,.1f} 且資金續強再看" if resistance else "重點盯，資金續強再看"
    elif status == "GIVE_UP":
        sentence = f"今日資金未轉強，暫時放棄"
    else:
        d = f"還差 {abs(distance_pct):.1f}%" if distance_pct is not None else "距離未知"
        sentence = f"距 {resistance:,.1f}{'（' + d + '）' if resistance else ''}，現在等" if resistance else "現在等"

    return dict(
        resistance=resistance, current=current, distance_pct=distance_pct,
        chip_summary=chip_summary, flow_display=flow_display, flow_stale=flow_stale,
        status=status, status_label=STATUS_LABEL[status], system_sentence=sentence,
        activation_prob=activation_prob, calibration_bucket=bucket,
        calibration_version=CALIBRATION_VERSION if bucket is not None else None,
        confirmed_so_far=confirmed_today,
    )
