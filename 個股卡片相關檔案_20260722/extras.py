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
import broker  # VPS Shioaji 訂閱 buffer


def _now_tw() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _raw_rows() -> List[Dict[str, Any]]:
    """從 broker buffer 拿真實 snap、餵給 vps_intraday_test._row 計算 group/aflow。
    跟 /api/intraday-test endpoint 共用同一邏輯。"""
    try:
        raw = broker.raw_buffer_snapshots()
        return [VIT._row(item) for item in raw]
    except Exception:
        return []


# ── /api/stock/{code} ──────────────────────────────────────
def build_stock_card(code: str) -> Dict[str, Any]:
    """組單檔個股決策卡。優先從 broker buffer 找該檔 snap；
    缺資料時降級到 stock_card.build_card 自己 fetch 行情。"""
    snap = None
    try:
        for item in broker.raw_buffer_snapshots():
            if str(item.get("code", "")) == str(code):
                snap = VIT._row(item)
                break
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
            "sector": C.SECTOR_MAP.get(code, ("其他",))[0],
            "live": snap or {}}


# ── /api/report ────────────────────────────────────────────
def build_report() -> Dict[str, Any]:
    """今日 / 昨日的盤後報告 — 從 broker buffer 拿 51 檔真實 rows。"""
    rows = _raw_rows()
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
        "count": len(rows),
        "note": "mls-intraday 盤中即時觀察池摘要；盤後正式報告由 after_hours 模組補完",
    }


# ── /api/watchpool ────────────────────────────────────────
def build_watchpool() -> Dict[str, Any]:
    """51 檔觀察池全集 — 從 broker buffer 抓真實 rows，
    沒回報的檔用 config 補上 name/sector。"""
    rows_map = {str(r.get("code", "")): r for r in _raw_rows()}
    try:
        subs = set(getattr(broker, "_SUBSCRIBED", set())) or set(C.UNIVERSE)
    except Exception:
        subs = set(C.UNIVERSE)

    items: List[Dict[str, Any]] = []
    for code in C.UNIVERSE:
        snap = rows_map.get(str(code), {})
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
            "volume_ratio": snap.get("volume_ratio"),
            "has_data": bool(snap.get("price")),
        })
    return {
        "ok": True,
        "updated_at": _now_tw(),
        "count": len(items),
        "items": items,
    }
