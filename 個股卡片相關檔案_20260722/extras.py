# -*- coding: utf-8 -*-
"""個股卡片 / 每日報告 / 51 檔觀察池 — 三個 UI 路由的資料供應端。

設計目標：
- /api/stock/{code}  → 從 build_card() 撈單張個股決策卡
- /api/report        → 今日 / 昨日的盤後驗證摘要（接 review + state）
- /api/watchpool     → 51 檔全集觀察池（從 /api/intraday-test 抓 rows）

不另存資料庫、全部從 VPS Shioaji 訂閱 buffer 與 mls_intraday.py
既有路由的 STATE 拼裝出來。
"""
from __future__ import annotations

import sys
import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Optional

# 保證 vps_intraday_test 與 app/ 都在 import 路徑
_BASE = Path(__file__).resolve().parent
_ROOT = _BASE.parent
for p in (str(_BASE), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config as C  # 0722 自己的 config
import stock_card
import vps_intraday_test as VIT  # 內含 51 檔 / Shioaji 訂閱查詢


def _now_tw() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# ── /api/stock/{code} ──────────────────────────────────────
def build_stock_card(code: str) -> Dict[str, Any]:
    """組單檔個股決策卡。優先吃 vps_intraday_test 內的 snap；
    缺資料時降級到 stock_card.build_card 自己 fetch 行情。"""
    snap = None
    try:
        snap = VIT._row({"code": code, "price": 0, "change_rate": 0,
                         "buy_volume": 0, "sell_volume": 0, "total_volume": 0})
    except Exception:
        snap = None
    try:
        card = stock_card.build_card(code, snap=snap)
    except Exception as e:
        return {"ok": False, "code": code, "error": f"build_card failed: {e}",
                "name": C.NAME_MAP.get(code, code),
                "sector": C.SECTOR_MAP.get(code, ("其他",))[0]}
    return {"ok": True, "code": code, "updated_at": _now_tw(),
            "card": card, "name": C.NAME_MAP.get(code, code),
            "sector": C.SECTOR_MAP.get(code, ("其他",))[0]}


# ── /api/report ────────────────────────────────────────────
def build_report() -> Dict[str, Any]:
    """今日 / 昨日的盤後報告。
    - 從 vps_intraday_test 的 _load 拿當下 STATE（51 檔狀態）
    - 從 /api/review 風格的本地 REVIEW 收集命中率（如果有）
    """
    try:
        # 從 vps_intraday_test 的內部邏輯重組（不依賴 FastAPI Request）
        import broker
        raw = broker.raw_buffer_snapshots()
        rows = [VIT._row(item) for item in raw]
    except Exception as e:
        rows = []

    # 簡易聚合：可操作 / 觀察 / 排除
    groups = {"可操作": 0, "觀察": 0, "排除": 0}
    for r in rows:
        g = r.get("group", "")
        if g in groups:
            groups[g] += 1

    return {
        "ok": True,
        "updated_at": _now_tw(),
        "asof_date": _dt.datetime.utcnow().strftime("%Y-%m-%d"),
        "rows": rows,
        "groups": groups,
        "note": "mls-intraday 盤中即時觀察池摘要；盤後正式報告由 after_hours 模組補完",
    }


# ── /api/watchpool ────────────────────────────────────────
def build_watchpool() -> Dict[str, Any]:
    """51 檔觀察池全集 — 直接從 config 拉、附上 Shioaji 即時報價。"""
    try:
        import broker
        subs = set(getattr(broker, "_SUBSCRIBED", set())) or set(C.UNIVERSE)
    except Exception:
        subs = set(C.UNIVERSE)

    items: List[Dict[str, Any]] = []
    for code in C.UNIVERSE:
        snap = {}
        try:
            snap = VIT._row({"code": code, "price": 0, "change_rate": 0,
                             "buy_volume": 0, "sell_volume": 0,
                             "total_volume": 0})
        except Exception:
            pass
        items.append({
            "code": code,
            "name": C.NAME_MAP.get(code, code),
            "sector": C.SECTOR_MAP.get(code, ("其他",))[0],
            "track": "engine" if code in getattr(C, "ENGINE_STOCKS", set()) else "attack",
            "subscribed": code in subs,
            "price": snap.get("price"),
            "change_rate": snap.get("change_rate"),
            "aflow": snap.get("aflow"),
            "group": snap.get("group"),
        })
    return {
        "ok": True,
        "updated_at": _now_tw(),
        "count": len(items),
        "items": items,
    }
