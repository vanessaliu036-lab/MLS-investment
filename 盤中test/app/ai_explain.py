# -*- coding: utf-8 -*-
"""
ai_explain.py — 每檔一行白話解讀（結合象限 / aflow / 三態 / 極端價 / 法人）

兩層設計（對齊「API key 空白要能降級」教訓）：
  第一層 local_explain()：純規則生一句話，免費、即時、必有輸出，永不開天窗。
  第二層 claude_explain()：可選，走 Claude API 潤飾成更自然的話；失敗自動退回第一層。

說人話原則：先講「這檔現在是什麼狀況」，再講「能不能碰、為什麼」。
不喊進出場價（那是狀態機/使用者的事），只做狀態翻譯與風險提示。

2026-07-20 華邦電教訓：以法人合計為主軸、分點大單為輔。
- 分點短線大單（aflow > 0）≠ 法人買超，分點承接永遠不能單獨撐起「強惜售」標籤
- 法人合計賣超 > 成交量 3% → 惜售軸直接屏蔽（即使 aflow 多正）
- 法人單一買賣超絕對值 > 成交量 1% → 警報（數據源可能錯）
- 法人單一買賣超絕對值 > 成交量 10% → 硬擋，惜售/真攻擊全降級
"""

from typing import Optional
from .intraday_filter import (
    StockSnap, passes_filters, proxy_quadrant, aflow_intensity,
    is_extreme_price, validate_inst_data, inst_sell_blocks_absorb,
    compute_inst_net_total, PASS, NO_DATA,
)


def _format_inst_note(s: StockSnap) -> str:
    """法人資料可讀摘要（缺資料→空字串）。"""
    net = compute_inst_net_total(s)
    if net is None:
        return ""
    if s.inst_foreign is None and s.inst_trust is None and s.inst_dealer is None:
        return ""
    return f"（法人合計 {net:+,} 張：外資 {s.inst_foreign or 0:+,}、投信 {s.inst_trust or 0:+,}、自營 {s.inst_dealer or 0:+,}）"


def local_explain(s: StockSnap, regime: Optional[str] = None) -> str:
    """純規則一行解讀，必有輸出。"""
    r = passes_filters(s, regime=regime)
    q = proxy_quadrant(s.aflow, s.change_rate)
    intensity = aflow_intensity(s.aflow, s.total_volume)
    inst_v = r.get("inst_validation", {})
    inst_blocked = r.get("inst_sell_blocks_absorb", False)
    net_total = r.get("inst_net_total")

    # 0) 法人數據硬擋優先（華邦電教訓：數量級異常時一切訊號降級）
    if inst_v.get("hard_block"):
        warns = inst_v.get("warnings", [])
        warn_txt = ("；" + " / ".join(warns[:2])) if warns else ""
        return (f"⚠️ 法人數據硬擋：{warn_txt}"
                f"aflow +{s.aflow:,} 與法人方向矛盾，所有訊號降級不可信，先別碰。")

    # 1) 極端價優先：訊號不可信，直接勸退
    if r["extreme"]:
        direction = "跌停" if s.change_rate < 0 else "漲停"
        return (f"逼近{direction}（{s.change_rate:+.1f}%），此時 aflow +{s.aflow:,} "
                f"多半是掛單被動成交、不是真吸籌，訊號全部降級不可信，先別碰。")

    # 2) 法人合計賣超屏蔽惜售（第二層強制排除）
    if inst_blocked and q == "惜售":
        inst_note = _format_inst_note(s)
        return (f"跌{s.change_rate:.1f}% 雖有大單承接（aflow +{s.aflow:,}），"
                f"但法人合計賣超 {net_total:+,} 張{inst_note}，"
                f"分點力道被法人調節蓋過，不算惜售格局，不建議抄底。")

    # 3) MA20 未接入：點出引擎軌暫時瞎眼
    ma20_nodata = r["states"].get("above_ma20") == NO_DATA

    # 4) 依象限給主軸
    if q == "真攻擊":
        head = f"漲{s.change_rate:+.1f}% 且買盤積極（aflow +{s.aflow:,}），屬真攻擊。"
        tail = "是追漲候選" if r["all_pass"] else "但未全數過關，追高留意風險"
    elif q == "惜售":
        strong = intensity is not None and intensity >= 10
        inst_note = _format_inst_note(s)
        if strong and net_total is not None and net_total >= 0:
            # 法人有買 + 分點吸籌 → 真正惜售
            head = (f"跌{s.change_rate:.1f}% 法人買盤進場（合計 {net_total:+,} 張{inst_note}），"
                    f"配合分點吸籌 {intensity:.0f}%，屬強惜售。")
            tail = "是跌深反彈候選，但屬抄底、要配緊停損"
        elif strong:
            # 分點強但法人沒資料 → 保守講
            head = (f"跌{s.change_rate:.1f}% 有人低接（吸籌佔比 {intensity:.0f}%），"
                    f"但法人資料未接入，無法確認是否真惜售。")
            tail = "先觀察法人動向，別急"
        else:
            head = f"跌{s.change_rate:.1f}% 有人低接，但吸籌力道普通，鑑別度不高。"
            tail = "先觀察，別急"
    elif q == "假紅":
        head = f"漲{s.change_rate:+.1f}% 但資金在流出（aflow {s.aflow:,}），是假紅。"
        tail = "衝高留意拉回，別追"
    else:  # 休息
        head = f"跌{s.change_rate:.1f}% 且資金流出（aflow {s.aflow:,}），量能休息。"
        tail = "沒訊號，略過"

    ma20_note = "（MA20 未接入，引擎軌進出場暫無法驗證）" if ma20_nodata else ""
    # 法人警告附加（不擋但提示）
    extra_warns = inst_v.get("warnings", [])
    warn_suffix = ""
    if extra_warns and not inst_v.get("hard_block"):
        # 只列第一條當提示，UI 完整版走 inst_warnings 欄
        first = extra_warns[0].split("，")[0].split("，")[0]
        warn_suffix = f" ⚠️{first}"
    return f"{head}{tail}。{ma20_note}{warn_suffix}"


def claude_explain(s: StockSnap, regime: Optional[str] = None,
                   api_key: Optional[str] = None) -> str:
    """
    可選：走 Claude API 潤飾。無 key 或失敗 → 自動退回 local_explain（不開天窗）。
    在 VPS 上呼叫；本測試環境無網路故僅示範結構。
    """
    base = local_explain(s, regime=regime)
    if not api_key:
        return base
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            "你是台股盤中助手。根據以下事實，用一句繁體中文白話說明這檔現在的狀況與"
            "能不能碰，不要喊買賣價，不要免責聲明：\n"
            f"代碼{s.code} 漲跌{s.change_rate:+.2f}% aflow{s.aflow:+} "
            f"象限{proxy_quadrant(s.aflow, s.change_rate)} 規則判讀「{base}」"
        )
        msg = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return text.strip() or base
    except Exception:
        return base   # 任何失敗都退回本地解讀
