# -*- coding: utf-8 -*-
"""隔離盤中測試服務。

這個服務只讀既有 MLS broker 的 Shioaji 訂閱 buffer，不寫資料庫、
不啟動第二組訂閱，也不改主站的 STATE。部署到 VPS 時可獨立跑在 8002。
"""

import sys
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import broker  # noqa: E402  (VPS 的既有真實行情連線)
import config  # noqa: E402
try:
    from mls_intraday import intraday_filter as F  # noqa: E402
except ImportError:
    from app import intraday_filter as F  # noqa: E402
try:
    from mls_intraday import ai_explain  # noqa: E402
except ImportError:
    from app import ai_explain  # noqa: E402
try:
    from mls_intraday import classify  # noqa: E402
except ImportError:
    from app import classify  # noqa: E402

router = APIRouter()
HISTORY_DB = BASE / "intraday_eod.db"


def _eod_module():
    """支援本機 mls_intraday 與 VPS app 兩種套件路徑。"""
    try:
        from mls_intraday import eod_stamp
    except ImportError:
        from app import eod_stamp
    return eod_stamp


def _history_ready(eod_stamp):
    """空的獨立 DB 也要能正常回傳空歷史，不得讓 API 500。"""
    if not HISTORY_DB.exists():
        return False
    import sqlite3
    with sqlite3.connect(HISTORY_DB) as conn:
        eod_stamp.ensure_table(conn)
    return True


def _row(raw):
    code = str(raw.get("code", ""))
    buy = int(raw.get("buy_volume") or 0)
    sell = int(raw.get("sell_volume") or 0)
    # broker 已把 buy/sell 正規化成 active_buy/active_sell；核心公式仍吃 raw bid/ask。
    aflow = F.aflow_official(sell, buy)
    change = float(raw.get("change_rate") or 0)
    price = float(raw.get("price") or 0)
    ma20 = None
    ma20_status = {}
    try:
        import server
        ma20 = server.get_ma20(code)
        ma20_status = server.ma20_cache_status()
    except Exception:
        pass
    snap = F.StockSnap(
        code=code,
        track="engine" if code in getattr(config, "ENGINE_STOCKS", set()) else "attack",
        price=price,
        change_rate=change,
        aflow=aflow,
        total_volume=int(raw.get("total_volume") or 0),
        ma20=ma20,
    )
    filters = F.passes_filters(snap, regime=_current_regime())
    classification = classify.classify_one(snap, regime=_current_regime())
    explanation = ai_explain.local_explain(snap, regime=_current_regime())
    return {
        "code": code,
        "name": getattr(config, "NAME_MAP", {}).get(code, code),
        "price": price,
        "change_rate": round(change, 2),
        "buy_volume": buy,
        "sell_volume": sell,
        "tick_type": raw.get("tick_type"),
        "raw_bid_side_total_vol": sell,
        "raw_ask_side_total_vol": buy,
        "aflow": aflow,
        "quadrant": F.proxy_quadrant(aflow, change),
        "total_volume": int(raw.get("total_volume") or 0),
        "ma20": ma20,
        "ma20_cache": ma20_status,
        "volume_ratio": raw.get("volume_ratio"),
        "filters": filters,
        "classification": classification,
        "group": classification["group"],
        "subgroup": classification["subgroup"],
        "classification_reason": classification["reason"],
        "filter_no_data": filters["no_data"],
        "extreme_price": filters["extreme"],
        "ai": explanation,
        "bidask_available": False,
    }


def _current_regime():
    """讀主站同 process 的溫度計，不另開行情連線。"""
    try:
        import server
        score = (server.STATE.get("market") or {}).get("score")
        if score is not None:
            return F.market_regime(int(score))
    except Exception:
        pass
    return F.REGIME_RANGE


@router.get("/api/intraday-test")
def intraday_test():
    started = time.time()
    try:
        raw = broker.raw_buffer_snapshots()
        regime = _current_regime()
        rows = [_row(item) for item in raw]
        # v5 分類攤平：可操作→觀察→排除；各群內仍維持漲幅優先，再按 aflow。
        group_order = {"可操作": 0, "觀察": 1, "排除": 2}
        rows.sort(key=lambda x: (group_order.get(x["group"], 9),
                                 -x["change_rate"], -x["aflow"]))
        category_counts = {}
        for row in rows:
            category_counts[row["group"]] = category_counts.get(row["group"], 0) + 1
        quota = None
        try:
            usage = broker.get_api().usage()
            quota = {
                "used_mb": round(usage.bytes / 1024 / 1024, 2),
                "limit_mb": round(usage.limit_bytes / 1024 / 1024, 0),
                "subscribed": len(getattr(broker, "_SUBSCRIBED", set())),
            }
        except Exception as exc:
            quota = {"error": str(exc)}
        return {
            "ok": True,
            "source": "VPS Shioaji subscription buffer",
            "read_only": True,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "count": len(rows),
            "rows": rows,
            "category_counts": category_counts,
            "regime": regime,
            "quota": quota,
            "latency_ms": round((time.time() - started) * 1000, 1),
            "notes": [
                "aflow 使用既有訂閱 buffer 的官方買賣盤累積量",
                "此頁不寫 mls.db、不改主站 STATE",
                "MA20 由盤前快取接入；快取尚未建立時標示無資料，不補造數字",
                f"v4 三態 filter：{regime}；極端價訊號降級 NO_DATA",
            ],
        }
    except Exception as exc:
        return {"ok": False, "source": "VPS Shioaji subscription buffer", "error": str(exc)}


@router.get("/intraday-test", response_class=HTMLResponse)
def home():
    return HTMLResponse((BASE / "intraday_decision.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store, max-age=0"})


@router.get("/intraday-test/v2", response_class=HTMLResponse)
def home_v2():
    """強制重整版 — URL 變了瀏覽器必抓新 HTML。"""
    return HTMLResponse((BASE / "intraday_decision.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store, max-age=0, must-revalidate"})


@router.get("/api/intraday-history/dates")
async def history_dates():
    eod_stamp = _eod_module()
    if not _history_ready(eod_stamp):
        return {"ok": True, "dates": [], "source": str(HISTORY_DB),
                "note": "尚未建立盤後蓋章資料"}
    return {"ok": True, "dates": eod_stamp.list_trade_dates(str(HISTORY_DB)),
            "source": str(HISTORY_DB)}


@router.get("/api/intraday-history")
async def history_rows(date: str, group: str = ""):
    eod_stamp = _eod_module()
    if not _history_ready(eod_stamp):
        return {"ok": True, "date": date, "rows": [],
                "note": "尚未建立盤後蓋章資料"}
    rows = eod_stamp.load_eod(str(HISTORY_DB), date, group or None)
    group_order = {"可操作": 0, "觀察": 1, "排除": 2}
    for row in rows:
        row["name"] = getattr(config, "NAME_MAP", {}).get(str(row["code"]), str(row["code"]))
        row["price"] = row.get("close_price")
        row["ai"] = (
            "極端價訊號不可信，請勿以 aflow 判定吸籌。"
            if row.get("extreme_price") else
            f"盤後分類為{row.get('group_name')}·{row.get('subgroup')}；"
            f"{row.get('quadrant')}，aflow {row.get('aflow'):+,}。"
        )
    rows.sort(key=lambda row: (group_order.get(row.get("group_name"), 9),
                               -(row.get("change_rate") or 0),
                               -(row.get("aflow") or 0)))
    return {"ok": True, "date": date, "rows": rows}


@router.get("/api/intraday-history/stock/{code}")
async def history_stock(code: str, days: int = 20):
    eod_stamp = _eod_module()
    if not _history_ready(eod_stamp):
        return {"ok": True, "code": code, "history": [],
                "trend": {"code": code, "trend": "無資料", "detail": ""}}
    return {"ok": True, "code": code,
            "history": eod_stamp.load_stock_history(str(HISTORY_DB), code, days),
            "trend": eod_stamp.stock_trend_summary(str(HISTORY_DB), code, days)}


@router.get("/intraday-history", response_class=HTMLResponse)
async def history_home():
    return (BASE / "history_page.html").read_text(encoding="utf-8")
