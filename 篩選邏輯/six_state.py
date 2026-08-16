"""六態分類器(2026-08-12 定案規格)。

把「背離5態 + 結構失效 + 觸發/動能」收斂成固定 6 態,供顯示端唯一分類。
規則:51 檔一律分到 6 態之一,零消失、零未分類。

  🔥 A級觸發   籌碼反轉 or 已觸發突破(資金↑量↑價↑齊揚)
  🟢 接近啟動   洗盤(量縮回檔待彈) or 攻擊軌接近觸發價
  🟢 轉強      抗賣壓 / 蓄勢吸籌(籌碼背離看多,價格尚未反應)
  🟡 等待      無明確訊號(預設)
  🟠 轉弱      買盤鈍化 / 高檔派發 / 籌碼價格雙殺(背離看空,但不淘汰)
  🔴 失效      僅限四重結構失效(真淘汰);鈍化/派發不得列此

鐵律:🔴 失效 ⇔ 真結構失效(未被背離救回)。買盤鈍化/派發最多 🟠 轉弱。
"""

from __future__ import annotations

# 態 key → (emoji 標籤, 排序權重: 越前越優先觀察)
STATE_META = {
    "trigger":   ("🔥 A級觸發", 0),
    "near":      ("🟢 接近啟動", 1),
    "strong":    ("🟢 轉強", 2),
    "wait":      ("🟡 等待", 3),
    "weakening": ("🟠 轉弱", 4),
    "failed":    ("🔴 失效", 5),
}

# 進「盤中觀察清單(核心層)」的態:🔥🟢🟢。其餘(等待/轉弱/失效)全池仍顯示但非核心。
CORE_STATES = ("trigger", "near", "strong")

_REJECTED = "淘汰"


def _near_trigger(item: dict) -> bool:
    """攻擊軌且現價已接近觸發價(差 <= 2%)= 接近啟動。"""
    trig = item.get("trigger_price") or item.get("entry_ref")
    close = item.get("close") or item.get("price")
    try:
        trig = float(trig); close = float(close)
    except (TypeError, ValueError):
        return False
    if trig <= 0:
        return False
    return -0.005 <= (trig - close) / trig <= 0.02   # 已站上或差 2% 內


def classify(item: dict) -> str:
    """回傳 6 態 key。輸入=build 產出的 item(含 divergence_type/classification/
    structural_failures/triggered/trigger_price 等)。任何輸入都回一個態,不回 None。"""
    div = item.get("divergence_type")
    cls = item.get("classification") or item.get("tier")
    rescued = bool(item.get("divergence_rescued"))
    triggered = bool(item.get("triggered"))

    # 🔴 失效:僅真四重結構失效(未被背離救回)
    if cls == _REJECTED and not rescued:
        return "failed"
    # 🔥 A級觸發:籌碼反轉 or 已觸發突破
    if div == "chip_reversal" or triggered:
        return "trigger"
    # 🟠 轉弱:買盤鈍化/高檔派發/雙殺(背離看空,不淘汰)
    if div in ("buying_stall", "double_weak"):
        return "weakening"
    # 🟢 接近啟動:洗盤(量縮回檔待彈)或攻擊軌接近觸發
    if div == "washout" or _near_trigger(item):
        return "near"
    # 🟢 轉強:抗賣壓/蓄勢吸籌(籌碼背離看多,價未反應)
    if div in ("sell_absorption", "accumulation"):
        return "strong"
    # 🟡 等待(預設,無明確訊號)
    return "wait"


def label(state_key: str) -> str:
    return STATE_META.get(state_key, STATE_META["wait"])[0]


def annotate(item: dict) -> dict:
    """就地補 state / state_label,回傳同一 dict。"""
    k = classify(item)
    item["state"] = k
    item["state_label"] = label(k)
    item["is_core"] = k in CORE_STATES
    return item


def completeness(items: list[dict], universe_size: int = 51) -> dict:
    """分類完整性(規格 API):任何股票不得消失。"""
    classified = sum(1 for it in items if it.get("state"))
    return {
        "total_universe": universe_size,
        "classified_count": classified,
        "unclassified_count": max(0, universe_size - classified),
        "classification_complete": classified >= universe_size,
        "by_state": {k: sum(1 for it in items if it.get("state") == k) for k in STATE_META},
    }
