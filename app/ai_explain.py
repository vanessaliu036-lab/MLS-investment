# -*- coding: utf-8 -*-
"""每檔一行白話解讀；預設使用本地規則，不依賴外部 API。"""

from typing import Optional

from .intraday_filter import (
    NO_DATA,
    StockSnap,
    aflow_intensity,
    is_extreme_price,
    passes_filters,
    proxy_quadrant,
)


def local_explain(s: StockSnap, regime: Optional[str] = None) -> str:
    """純規則產生一行解讀，確保沒有 API key 時仍有結果。"""
    result = passes_filters(s, regime=regime)
    quadrant = proxy_quadrant(s.aflow, s.change_rate)
    intensity = aflow_intensity(s.aflow, s.total_volume)

    if result["extreme"]:
        direction = "跌停" if s.change_rate < 0 else "漲停"
        return (f"逼近{direction}（{s.change_rate:+.1f}%），委託結構可能令 aflow 失真，"
                "訊號降級不可信，先觀察。")

    if quadrant == "真攻擊":
        head = f"漲{s.change_rate:+.1f}% 且買盤積極（aflow {s.aflow:+,}），屬真攻擊。"
        tail = "條件全過，可列入追蹤。" if result["all_pass"] else "尚未全過，追高留意風險。"
    elif quadrant == "惜售":
        if intensity is not None and intensity >= 10:
            head = f"跌{s.change_rate:.1f}% 但有承接（吸籌佔比 {intensity:.1f}%），屬強惜售。"
            tail = "可列入跌深反彈觀察，需配合停損。"
        else:
            head, tail = "下跌中有低接但力道普通。", "先觀察。"
    elif quadrant == "假紅":
        head, tail = f"漲{s.change_rate:+.1f}% 但資金流出，屬假紅。", "留意拉回，避免追高。"
    else:
        head, tail = f"跌{s.change_rate:.1f}% 且資金流出，量能偏弱。", "目前沒有明確訊號。"

    if result["states"].get("above_ma20") == NO_DATA:
        tail += " MA20 尚未接入，引擎軌暫無法驗證。"
    return head + tail


def claude_explain(s: StockSnap, regime: Optional[str] = None,
                   api_key: Optional[str] = None) -> str:
    """可選外部潤飾；沒有 key 或呼叫失敗時自動回退本地解讀。"""
    base = local_explain(s, regime)
    if not api_key:
        return base
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=120,
            messages=[{"role": "user", "content":
                       f"用一句繁體中文解讀台股狀況，不喊買賣價：{base}"}],
        )
        text = "".join(block.text for block in msg.content
                       if getattr(block, "type", "") == "text").strip()
        return text or base
    except Exception:
        return base
