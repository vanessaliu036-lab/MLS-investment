# -*- coding: utf-8 -*-
"""explain.py — 說明語意層(單一事實來源)。詳見專案根目錄 說明語意層規格.md。

定位:後台把「已經算好的結構化事實」翻成「一句你看得懂的白話」。
  · 不做篩選:這裡拿到的 row 早就被真正的漏斗判定過了,本檔只負責「怎麼講」。
  · 不吃分數當語意驅動:淘汰理由一律照 row 真實的 fail_factor/detail 走,
    七因子總分只當附註,絕不用 score<40 現算「追高風險最高」那種話
    (那正是前端三套現算、同篩選不同說法的病灶)。
  · 全站唯一嘴巴:前端 rejectExplain()/plain()、後端 ai_explain 都改引用這裡,
    改一句話只改這份對照表一處。

輸出合約(對齊規格 §3):
    { "facts": {...}, "tier": "...", "tier_label": "...",
      "explain": "一句白話", "tags": ["最多三個標籤"] }
"""

from __future__ import annotations

from typing import Any, Optional

NO_DATA = None  # 籌碼未到 = Pending,不加不扣,不得當負面(規格 §4.2)

# ── 分層(取代淘汰制;此處為「顯示分層」,不是新篩選)──────────────
TIER_CORE = "CORE"
TIER_STRONG_NO_CHASE = "STRONG_NO_CHASE"
TIER_CANDIDATE = "CANDIDATE"
TIER_REJECTED = "REJECTED"
TIER_LABEL = {
    TIER_CORE: "核心觀察",
    TIER_STRONG_NO_CHASE: "強勢觀察｜禁追",
    TIER_CANDIDATE: "候補觀察",
    TIER_REJECTED: "淘汰",
}


def _num(v: Any) -> Optional[float]:
    """容錯轉數字;None/空字串/非數 → None,絕不拿 0 頂替缺值。"""
    if v is None or v == "":
        return NO_DATA
    try:
        return float(v)
    except (TypeError, ValueError):
        return NO_DATA


def _fmt_lots(n: Optional[float]) -> str:
    if n is None:
        return "—"
    return f"{n:+,.0f}"


# ══════════════════════════════════════════════════════════
# 1. 把原始 row 正規化成 facts(綁真實欄位,見 db.watch_reject / ab watchlist)
# ══════════════════════════════════════════════════════════
def build_facts(row: dict) -> dict:
    """從真實 row 抽出結構化事實。欄位對齊 load_watch_rejects 的 SELECT *。

    缺的欄位一律 NO_DATA,不臆造;有的才講。
    """
    change = _num(row.get("today_change", row.get("change_rate")))
    flow = _num(row.get("today_aflow", row.get("aflow")))
    return {
        "code": row.get("stock_id") or row.get("code"),
        "name": row.get("stock_name") or row.get("name") or row.get("stock_id") or row.get("code"),
        "sector": row.get("sector"),
        "source": row.get("source"),
        # 盤中即時
        "change_rate": change,
        "flow": flow,                                   # 主動買賣差(張),+流入 -流出
        "flow_ratio": _num(row.get("flow_ratio")),      # 佔成交量比(有才用)
        "sector_strong": row.get("sector_strong"),      # None=未判定
        # 盤後籌碼
        "inst_net": _num(row.get("inst_net")),
        "inst_streak": _num(row.get("consecutive_days", row.get("inst_streak"))),
        "inst_status": row.get("inst_status", "pending" if row.get("inst_net") is None else None),
        "support_state": row.get("support_state"),      # hold/break/near/None
        "turning": row.get("turning"),
        "score_total": _num(row.get("score_total", row.get("factor_score"))),
        # 真實淘汰依據(照它走,不現算)
        "fail_factor": (row.get("fail_factor") or "").strip(),
        "detail": (row.get("detail") or row.get("reason") or "").strip(),
    }


# ══════════════════════════════════════════════════════════
# 2. 白話標籤(規格 §4.1/§4.2):有事實才給,最多三個
# ══════════════════════════════════════════════════════════
def build_tags(f: dict) -> list[str]:
    tags: list[str] = []
    if f["sector_strong"] is True and f["sector"]:
        tags.append(f"{f['sector']}族群續強")
    flow = f["flow"]
    if flow is not None:
        if flow > 0:
            tags.append(f"主動流入 {_fmt_lots(flow)}")
        elif flow < 0:
            tags.append(f"主動流出 {_fmt_lots(flow)}")
    streak = f["inst_streak"]
    if streak is not None and streak >= 2:
        tags.append(f"近 {int(streak)} 日法人連買")
    elif streak is not None and streak <= -2:
        tags.append(f"近 {int(abs(streak))} 日法人連賣")
    elif f["inst_status"] == "pending":
        tags.append("盤後籌碼待補")
    if f["support_state"] == "hold" and len(tags) < 3:
        tags.append("支撐未破")
    return tags[:3]


# ══════════════════════════════════════════════════════════
# 3. 淘汰說明(照 fail_factor/detail 真實理由翻,不吃分數決定語意)
#    這支直接取代前端 rejectExplain() 的 if score<40 現算。
# ══════════════════════════════════════════════════════════
def render_reject_explain(f: dict) -> str:
    # 不掛分數:七因子總分對使用者沒意義、只會跟白話結論打架(2026-08-07 定案移除)。
    # 淘汰語意一律照真實 fail_factor/detail 走。
    detail = f["detail"]
    factor = f["fail_factor"]
    blob = f"{factor} {detail}"

    # 漲停:強勢股不是「沒價值」,是「現在不宜追」→ 禁追、等回測(規格 §7、誤刪率規格)
    if "漲停" in blob:
        return "強勢漲停但乖離高、籌碼未定案 → 保留觀察、禁開盤追,等回測昨高或 5MA。"
    if "假紅" in blob:
        return "拉高卻主動賣超 → 主力邊拉邊出,假突破,避開。"
    if "資金流出" in blob:
        return "量價同步走弱 → 資金離場。"
    if "量比" in blob:
        return "量能未放大(量比不足) → 缺續攻動能,續觀察。"
    if "法人" in blob and ("<=0" in blob or "賣" in blob):
        return "法人未站買方 → 籌碼未同步,不主動進場。"
    # 門檻語意優先於名額:真實 fail_factor 常是「觀察未達門檻/名額不足」複合字串,
    # 但畫面上該檔顯示的是「條件待確認」,故 detail 帶門檻字樣時以門檻為準。
    if any(k in blob for k in ("條件待確認", "未達門檻", "未達選股門檻")):
        return "未達入選門檻 → 續觀察,不主動進場。"
    if "名額" in blob:
        return "結構過關但名額被更強者佔走 → 候補,盤中觸發再升級。"
    # 有真實 detail 就照講;真的什麼都沒有才給保守句
    if detail:
        return detail
    return "盤後條件未達標 → 續觀察。"


# ══════════════════════════════════════════════════════════
# 4. 盤中即時說明(規格 §4.1:群組續強 + 資金流入為主軸)
#    取代前端 plain() 的 if score<40 現算。
# ══════════════════════════════════════════════════════════
def render_intraday_explain(f: dict) -> str:
    ch, flow = f["change_rate"], f["flow"]
    if ch is None or flow is None:
        return "尚未收到即時行情,暫時無法判讀。"
    # 漲停委託結構會讓資金流失真 → 降級觀察(對齊 classify 極端價規則)
    if ch >= 9.8:
        return "逼近漲停,委託結構恐令資金流失真 → 訊號降級,先觀察不追。"
    if ch <= -9.8:
        return "逼近跌停,訊號失真 → 先觀察。"
    if ch > 0 and flow > 0:
        return "價漲＋主動買盤進場 → 量價同步,買氣真實。"
    if ch > 0 and flow < 0:
        return "拉高卻主動賣超 → 主力邊拉邊出,假紅要防。"
    if ch < 0 and flow < 0:
        return "量價同步走弱 → 資金離場。"
    if ch < 0 and flow > 0:
        return "下跌但有承接 → 逢低買盤進場,觀察能否止跌。"
    if flow > 0:
        return "價平但買盤流入 → 有人默默布局,續盯。"
    if flow < 0:
        return "價平但賣壓流出 → 動能轉弱,保守看待。"
    return "量價中性、方向未明 → 續觀察。"


# ══════════════════════════════════════════════════════════
# 5. 對外總入口:一列 → 完整說明合約
# ══════════════════════════════════════════════════════════
def explain_row(row: dict, kind: str = "reject") -> dict:
    """kind='reject' 淘汰列 / 'intraday' 盤中列。回傳規格 §3 合約。"""
    f = build_facts(row)
    explain = render_reject_explain(f) if kind == "reject" else render_intraday_explain(f)
    # tier 目前為顯示分層:淘汰列裡「漲停/名額不足」屬強勢禁追或候補,不是真失效
    blob = f"{f['fail_factor']} {f['detail']}"
    if kind == "reject":
        if "漲停" in blob:
            tier = TIER_STRONG_NO_CHASE
        elif "名額" in blob:
            tier = TIER_CANDIDATE
        else:
            tier = TIER_REJECTED
    else:
        tier = TIER_CANDIDATE
    return {
        "facts": f,
        "tier": tier,
        "tier_label": TIER_LABEL[tier],
        "explain": explain,
        "tags": build_tags(f),
    }


# ══════════════════════════════════════════════════════════
# 自我驗證:用截圖三檔真實數字跑一遍,證明欄位接得上、句子對得上。
#   python3 個股卡片相關檔案_20260722/explain.py
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    samples = [
        {"stock_id": "2049", "stock_name": "上銀", "sector": "無人機",
         "today_change": 8.01, "today_aflow": 2814, "sector_strong": True,
         "fail_factor": "排除", "detail": "漲停", "score_total": 34},
        {"stock_id": "6213", "stock_name": "聯茂", "sector": "PCB材料",
         "today_change": 4.4, "today_aflow": 4779, "sector_strong": True,
         "fail_factor": "排除", "detail": "漲停", "score_total": 36},
        {"stock_id": "8046", "stock_name": "南電", "sector": "ABF載板",
         "today_change": 1.35, "today_aflow": 293,
         "fail_factor": "觀察未達門檻/名額不足", "detail": "條件待確認", "score_total": 24},
    ]
    print("=== 說明語意層 · 真實資料驗證(截圖三檔)===\n")
    for s in samples:
        out = explain_row(s, kind="reject")
        f = out["facts"]
        print(f"[{f['code']} {f['name']}] {f['sector']}  今日{f['change_rate']:+}%  資金{_fmt_lots(f['flow'])}")
        print(f"  舊前端(score現算): 純動能、無籌碼支撐 → 追高風險最高")
        print(f"  新說明合約 tier   : {out['tier_label']}")
        print(f"  新說明合約 explain: {out['explain']}  (無分數)")
        print(f"  新說明合約 tags   : {out['tags']}")
        print()
