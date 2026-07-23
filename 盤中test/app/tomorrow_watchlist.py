# -*- coding: utf-8 -*-
"""
tomorrow_watchlist.py — 盤後產生明日觀察清單

核心原則（你系統的話）：盤中資料負責「今天能不能追」，盤後資料負責「明天值不值得等」。

流程：
  1. 讀今天盤中收盤分類（intraday_eod：可操作/觀察）。
  2. 疊加 TWSE 盤後籌碼（三大法人/融資），驗證是否延續。
  3. 判四狀態 Ready / Watch / Pullback Watch / Pass，輸出明日清單。

務實原則（依你要求）：
  - 只用「確定抓得到」的：盤中分類(已存) + MA(kbars自算) + TWSE官方法人/融資。
  - TWSE 抓不到 → 該欄標 NO_DATA、明日狀態最多到 Watch，不會因缺料崩潰。
  - 券商分點/主力方向：資料源未接 → 一律 NO_DATA，標「加分項·未接入」小字，不影響主流程。

準度備註（小字，UI 呈現）：
  - 法人為當日淨額；連買天數由歷史累計，首次執行無歷史則從今日起算。
  - 融資增減以 TWSE 當日餘額差計；接了券商分點會更能分辨換手/倒貨（尚未接入）。
"""

from typing import List, Dict, Optional
from .intraday_filter import StockSnap, aflow_intensity
from .eod_stamp import load_eod, load_stock_history

# 四狀態（沿用系統既有語彙）
READY = "Ready"                 # 明日可進場候選
WATCH = "Watch"                 # 等回測或突破確認
PULLBACK_WATCH = "Pullback Watch"  # 爆量追價風險高，等回踩均價/MA5
PASS = "Pass"                   # 移出清單


def _fmt_num(v) -> str:
    return "—" if v is None else f"{v:+,}"


def build_watchlist(
    db_path: str,
    trade_date: str,
    institutional: Optional[Dict[str, dict]] = None,   # TWSE 法人，抓不到給 None
    margin: Optional[Dict[str, dict]] = None,          # TWSE 融資，抓不到給 None
    streak_prev: Optional[Dict[str, int]] = None,      # 各檔昨日連買天數
) -> List[dict]:
    """
    產明日觀察清單。只納入今日「可操作 / 觀察」兩群（排除群不進明日清單）。
    回傳每檔一列，含固定欄位 + 明日狀態 + 資料完整度。
    """
    institutional = institutional or {}
    margin = margin or {}
    streak_prev = streak_prev or {}

    # 只取今日可操作 + 觀察
    candidates = (load_eod(db_path, trade_date, "可操作")
                  + load_eod(db_path, trade_date, "觀察"))

    watchlist = []
    for row in candidates:
        code = row["code"]
        inst = institutional.get(code)          # None = TWSE 未抓到
        mg = margin.get(code)

        # 籌碼欄（抓不到標 None → UI 顯示「—」，不補造）
        foreign = inst["foreign"] if inst else None
        trust = inst["trust"] if inst else None
        inst_total = inst["total"] if inst else None
        margin_change = mg["margin_change"] if mg else None

        # 連買天數：有今日法人才累計，否則沿用昨日
        prev_streak = streak_prev.get(code, 0)
        if inst_total is not None:
            from .twse_fetch import accumulate_streak
            streak = accumulate_streak(inst_total, prev_streak)
        else:
            streak = prev_streak

        state, reason = _decide_state(row, inst_total, margin_change)

        # 資料完整度：籌碼有沒有抓到，決定判斷可信度
        chip_ok = inst is not None
        completeness = "完整" if chip_ok else "缺籌碼(僅盤中分類)"

        watchlist.append({
            "code": code,
            "close": row["close_price"],
            "change_rate": row["change_rate"],
            "intraday_group": row["group_name"],       # 今日盤中分類
            "intraday_sub": row["subgroup"],
            "quadrant": row["quadrant"],
            "aflow": row["aflow"],
            "aflow_intensity": row["aflow_intensity"],
            "foreign": foreign,                        # None → UI「—」
            "trust": trust,
            "inst_total": inst_total,
            "inst_streak": streak,
            "margin_change": margin_change,
            # 加分項（未接入資料源）→ 一律 NO_DATA，不影響主流程
            "broker_concentration": None,              # 券商集中度（未接入）
            "main_force": None,                        # 主力方向（未接入）
            "tomorrow_state": state,
            "reason": reason,
            "data_completeness": completeness,
        })

    # 排序：Ready → Pullback Watch → Watch → Pass；同狀態法人淨額大者在前
    order = {READY: 0, PULLBACK_WATCH: 1, WATCH: 2, PASS: 3}
    watchlist.sort(key=lambda r: (order.get(r["tomorrow_state"], 9),
                                  -(r["inst_total"] or -1e9)))
    return watchlist


def _decide_state(row: dict, inst_total: Optional[int],
                  margin_change: Optional[int]) -> tuple:
    """
    四狀態判定。籌碼抓不到時保守：最多到 Watch，不給 Ready（避免無籌碼驗證就進場）。

      Ready         : 今日可操作 + 法人買超 + 融資沒暴增
      Pullback Watch: 今日爆量上漲（真攻擊強勢）但追價風險高 → 等回踩
      Watch         : 價格強但籌碼分歧、或籌碼未抓到待確認
      Pass          : 法人賣超 or 融資暴增（主力倒貨疑慮）
    """
    group = row["group_name"]
    quadrant = row["quadrant"]

    # 籌碼未抓到 → 保守 Watch
    if inst_total is None:
        return WATCH, "籌碼未接入，待盤後法人確認（僅盤中分類）"

    # 法人賣超 → Pass
    if inst_total < 0:
        return PASS, "法人賣超，明日剔除"
    # 融資暴增（>0 且明顯）→ Pass（主力倒貨給散戶疑慮）
    if margin_change is not None and margin_change > 0 and inst_total < margin_change:
        return PASS, "融資暴增於法人買超，追價風險高"

    # 法人買超 + 今日可操作
    if group == "可操作" and inst_total > 0:
        if quadrant == "真攻擊":
            # 真攻擊強勢但要防追高
            return PULLBACK_WATCH, "強勢真攻擊+法人買超，等回踩均價/MA5再進"
        return READY, "可操作+法人買超+融資健康，明日可進場候選"

    # 觀察群 + 法人買超 → Watch
    return WATCH, "價格有亮點+法人買超，等突破或回測確認"
