"""
MLS 標準版 — gatekeeper.py
現金閘門:對應「40–50% 現金保留」紀律與華邦電教訓。
持股與當日交易次數由使用者維護 positions.json(同目錄):
{
  "positions": {"2408": {"qty": 2000, "avg": 60.0}},
  "daily_trade_count": 3
}
滿手(>= MAX_POSITIONS)或當日交易達上限(>= MAX_DAILY_TRADE)時:
  · 進場訊號仍計算、仍寫入 SQLite(供學習)
  · 但推播降級:訊息附「⚠️ 現金閘門」註記,前端進場列標記 gated
  · 出場/風險訊號永遠正常推播,不受閘門影響
"""

import os
import json

from config import MAX_POSITIONS, MAX_DAILY_TRADE

POS_FILE = os.path.join(os.path.dirname(__file__), "positions.json")


def load_positions():
    try:
        with open(POS_FILE) as f:
            d = json.load(f)
        return d.get("positions", {}), int(d.get("daily_trade_count", 0))
    except FileNotFoundError:
        # 首次執行自動建立空範本,供使用者編輯(閘門需填持股才會生效)
        try:
            with open(POS_FILE, "w") as f:
                json.dump({"positions": {}, "daily_trade_count": 0},
                          f, ensure_ascii=False, indent=2)
            print(f"[gatekeeper] 已建立 {POS_FILE},填入持股後現金閘門才會生效")
        except Exception:
            pass
        return {}, 0
    except Exception:
        return {}, 0


def gate_status():
    """回傳 (gated: bool, note: str, positions: dict)"""
    pos, cnt = load_positions()
    if len(pos) >= MAX_POSITIONS:
        return True, f"現金閘門:持股已達 {len(pos)}/{MAX_POSITIONS} 檔上限,進場訊號僅供記錄", pos
    if cnt >= MAX_DAILY_TRADE:
        return True, f"現金閘門:當日交易已達 {cnt}/{MAX_DAILY_TRADE} 次上限,進場訊號僅供記錄", pos
    return False, "", pos


def apply_gate(stocks):
    """
    對訊號列表套用閘門:
      · 進場訊號 → 加 gated / gate_note
      · 持股中的股票 → action 標 hold(除非本身是 sell 風險)
    """
    gated, note, pos = gate_status()
    for s in stocks:
        if s["code"] in pos and s["action"] != "sell":
            s["action"] = "hold"
            s["is_mine"] = True
        elif s["code"] in pos:
            s["is_mine"] = True
        if gated and s["action"] == "buy":
            s["gated"] = True
            s["gate_note"] = note
    return stocks, gated, note
