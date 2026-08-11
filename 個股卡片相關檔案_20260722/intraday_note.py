# -*- coding: utf-8 -*-
"""intraday_note.py — 淘汰名單的「今日盤中說明」(唯讀語意層)。

後台算事實、前台只印:把「淘汰當日的理由」對照「今日盤中資金流/漲跌」,
講一句「背離(誤刪候選)/ 確認(淘汰對了)」的白話。不做任何篩選、不寫表。

判斷主軸(對齊誤刪率量測 reject_verify/layered_backtest):
  淘汰理由多為偏空(賣超/收黑/跌破/流出/落後);若今日盤中資金翻為
  主動流入且價漲 → 資金回補、與淘汰理由背離 = 誤刪候選,該被看見。
  反之今日續弱、主動流出 → 淘汰方向獲當日盤中確認。
"""
from __future__ import annotations

from typing import Optional

# 偏空的淘汰理由關鍵詞:命中任一 → 該檔是「因為看空被刷掉」。
_BEARISH = ("賣超", "收黑", "流出", "跌破", "失效", "落後", "轉弱", "假紅", "走弱")


def _lots(n: Optional[float]) -> str:
    return f"{n:+,.0f}" if n is not None else "—"


def build(why: str, flow: Optional[float], change: Optional[float]) -> str:
    """why=淘汰理由(raw);flow=今日主動買賣差(張,+流入 -流出);change=今日漲跌幅(%)。

    缺資料不臆造,直說待補。回傳一句白話。
    """
    if flow is None or change is None:
        return "今日盤中資料未到,待開盤後判讀。"

    bearish = any(k in (why or "") for k in _BEARISH)
    lots = _lots(flow)
    pct = f"{change:+.2f}%"

    # 逼近漲停:委託結構會讓主動買賣差失真 → 一律降級、禁開盤追。
    if change >= 9.8:
        if bearish and flow > 0:
            return (f"淘汰後今日逼近漲停({pct})、主動流入 {lots} 張 → 資金強勢回補、"
                    f"與淘汰理由背離,但委託失真禁開盤追,等回測。")
        return f"今日逼近漲停({pct})、委託結構恐令資金失真 → 強勢但禁追,等回測。"

    # 背離:看空被刷掉、今日卻資金回補 → 誤刪候選(最該被看見的一類)。
    if bearish and flow > 0 and change > 0:
        return (f"淘汰因偏空(賣超/收黑);今日盤中翻為主動流入 {lots} 張、價 {pct} "
                f"→ 資金回補、與淘汰理由背離,列入誤刪觀察。")
    if bearish and flow > 0 and change <= 0:
        return (f"今日主動流入 {lots} 張但價未揚({pct}) → 有人低接、淘汰理由鬆動,"
                f"待價格確認。")

    # 確認:今日續弱 → 淘汰淘對。
    if flow < 0 and change < 0:
        return f"今日續弱、主動流出 {lots} 張、價 {pct} → 淘汰方向獲今日盤中確認。"
    if flow < 0 and change > 0:
        return (f"價漲 {pct} 卻主動賣超 {lots} 張 → 假紅、主力邊拉邊出,"
                f"淘汰方向未被推翻。")

    # 其餘(非偏空理由,或中性):照今日量價據實講。
    if flow > 0 and change > 0:
        return f"今日主動流入 {lots} 張、價 {pct} → 量價同步走強,有回補跡象。"
    if flow > 0:
        return f"價平但主動流入 {lots} 張 → 有人默默布局,續盯。"
    if flow < 0:
        return f"價平但主動流出 {lots} 張 → 動能轉弱,保守看待。"
    return "今日量價中性、方向未明 → 續觀察。"


if __name__ == "__main__":
    # 用截圖六檔 08-07 淘汰(偏空理由)＋今日盤中真值自我驗證
    samples = [
        ("跌破月線、法人賣超且收黑", 11174, 9.75),   # 5347 世界
        ("跌破月線、法人賣超且收黑", 20525, 8.64),    # 2337 旺宏
        ("法人賣超且收黑、族群明顯落後", 990, 9.9),    # 3026 禾伸堂
        ("跌破月線、法人賣超且收黑", -1500, -3.2),     # 假想:今日續弱
    ]
    print("=== 今日盤中說明 · 自我驗證 ===\n")
    for why, flow, chg in samples:
        print(f"[淘汰理由] {why}  今日 flow{flow:+,} 價{chg:+}%")
        print(f"  → {build(why, flow, chg)}\n")
