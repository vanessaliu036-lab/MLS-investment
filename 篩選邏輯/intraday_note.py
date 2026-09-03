# -*- coding: utf-8 -*-
"""intraday_note.py — 淘汰名單的「今日盤中說明」(唯讀語意層)。

後台算事實、前台只印:把「淘汰當日的理由」對照「今日盤中 A-flow/漲跌」,
講一句「背離(誤刪候選)/ 確認(淘汰對了)」的白話。不做任何篩選、不寫表。

A-flow = 今日主動買成交張數 − 主動賣成交張數，單位張；
它是盤中成交積極度估算，不是法人買賣超。
"""
from __future__ import annotations

from typing import Optional

# 偏空的淘汰理由關鍵詞可能來自「昨日官方法人賣超」或價格結構；
# 因此保留「賣超」做歷史理由比對，但盤中 flow 文案一律稱 A-flow/主動流入流出。
_BEARISH = ("賣超", "收黑", "流出", "跌破", "失效", "落後", "轉弱", "假紅", "走弱")


def _lots(n: Optional[float]) -> str:
    return f"{n:+,.0f}" if n is not None else "—"


def build(why: str, flow: Optional[float], change: Optional[float]) -> str:
    """why=淘汰理由(raw);flow=今日 A-flow(張,+流入 -流出);change=今日漲跌幅(%)。"""
    if flow is None or change is None:
        return "今日盤中資料未到,待開盤後判讀。"

    bearish = any(k in (why or "") for k in _BEARISH)
    lots = _lots(flow)
    pct = f"{change:+.2f}%"

    # 逼近漲停:委託結構會讓主動買賣差失真 → 一律降級、禁開盤追。
    if change >= 9.8:
        if bearish and flow > 0:
            return (f"淘汰後今日逼近漲停({pct})、A-flow {lots} 張 → 主動資金強勢回補、"
                    f"與淘汰理由背離,但委託失真禁開盤追,等回測。")
        return f"今日逼近漲停({pct})、委託結構恐令 A-flow 失真 → 強勢但禁追,等回測。"

    # 背離:看空被刷掉、今日卻資金回補 → 誤刪候選。
    if bearish and flow > 0 and change > 0:
        return (f"淘汰因偏空(昨日籌碼/價格結構);今日 A-flow {lots} 張、價 {pct} "
                f"→ 主動資金回補、與淘汰理由背離,列入誤刪觀察。")
    if bearish and flow > 0 and change <= 0:
        return (f"今日 A-flow {lots} 張但價未揚({pct}) → 有主動承接、淘汰理由鬆動,"
                f"待價格確認。")

    # 確認:今日續弱 → 淘汰方向獲盤中佐證。
    if flow < 0 and change < 0:
        return f"今日續弱、A-flow {lots} 張、價 {pct} → 淘汰方向獲今日盤中佐證。"
    if flow < 0 and change > 0:
        return (f"價漲 {pct} 但 A-flow {lots} 張為負 → 價流背離、短線假紅風險，"
                f"淘汰方向未被推翻。")

    # 其餘(非偏空理由,或中性):照價格 × A-flow 據實講。
    if flow > 0 and change > 0:
        return f"今日 A-flow {lots} 張、價 {pct} → 價流同步走強,有回補跡象。"
    if flow > 0:
        return f"價平但 A-flow {lots} 張為正 → 有主動承接,續盯。"
    if flow < 0:
        return f"價平但 A-flow {lots} 張為負 → 動能轉弱,保守看待。"
    return "今日價格與 A-flow 中性、方向未明 → 續觀察。"


if __name__ == "__main__":
    samples = [
        ("跌破月線、法人賣超且收黑", 11174, 9.75),
        ("跌破月線、法人賣超且收黑", 20525, 8.64),
        ("法人賣超且收黑、族群明顯落後", 990, 9.9),
        ("跌破月線、法人賣超且收黑", -1500, -3.2),
    ]
    print("=== 今日盤中說明 · 自我驗證 ===\n")
    for why, flow, chg in samples:
        print(f"[淘汰理由] {why}  今日 A-flow {flow:+,} 張 價{chg:+}%")
        print(f"  → {build(why, flow, chg)}\n")
