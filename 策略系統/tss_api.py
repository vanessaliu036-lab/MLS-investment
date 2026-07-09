"""
tss_api.py — TSS v1.0 獨立 FastAPI 服務

設計原則:
    - 獨立 port (預設 8765),不動主系統 mls-legacy server.py
    - 端點設計對齊 spec 一對一,curl 即可驗收
    - 含 lifespan 自動背景跑盤後篩選 (每日 13:40 觸發,簡化版)

端點:
    GET  /api/tss/health              健康檢查
    GET  /api/tss/watchlist           讀 mls.db watchlist (盤中即時可拿)
    POST /api/tss/run                 觸發盤後篩選 (background task)
    GET  /api/tss/last                看最近一次篩選結果 (記憶體快取)

啟動:
    cd /opt/pos-v1.2/策略系統
    uvicorn tss_api:app --host 0.0.0.0 --port 8765

驗證:
    curl http://127.0.0.1:8765/api/tss/health
    curl http://127.0.0.1:8765/api/tss/watchlist | python3 -m json.tool
"""

from __future__ import annotations

import os
import sys
import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from tss_mvp import (
    PARAMS,
    fetch_1min_kbars,
    fetch_index_daily,
    fetch_institutional,
    fetch_big_holder,
    classify_buy_sell_vol,
    filter_after_market,
    generate_mock_1m_kbars,
    mock_index_daily,
    mock_institutional,
    get_universe,
    load_dotenv_from_money_health,
    write_watchlist_to_mls_db,
    read_watchlist_from_mls_db,
    shioaji_login,
)

# ── 啟動時自動載入 .env (跟 mls-legacy 共用) ──
load_dotenv_from_money_health()

# ── 記憶體快取最近一次篩選結果 (給 /api/tss/last 用) ──
_LAST_RESULT: Dict[str, Any] = {
    "ts": None,
    "trade_date": None,
    "watchlist": {},
    "qualified": [],
    "status": "never_run",
}


def _do_filter(dry_run: bool, days: int) -> Dict[str, Any]:
    """
    跑一次盤後篩選,回傳結果 dict,並更新 _LAST_RESULT。
    跟 tss_scheduler.run_after_market_filter() 共用邏輯,這邊再精簡。
    """
    codes = get_universe()
    print(f"[tss_api] 篩選啟動 ({len(codes)} 檔)")

    if dry_run:
        index_df = mock_index_daily(days=120)
        inst_df_map = {c: mock_institutional(c, days=days) for c in codes}
        bh_df_map = {}
        stock_1m_map = {c: generate_mock_1m_kbars(days=days) for c in codes}
    else:
        api = shioaji_login()
        try:
            contract_map = {c: api.Contracts.Stocks.TSE[c] for c in codes}
            index_df = fetch_index_daily(api, days=120)
            inst_df_map = {c: fetch_institutional(c, days=max(days, 30)) for c in codes}
            bh_df_map = {c: fetch_big_holder(c) for c in codes}
            end = datetime.now()
            start = end - timedelta(days=days)
            stock_1m_map = {
                c: fetch_1min_kbars(api, contract_map[c], start, end) for c in codes
            }
        finally:
            try:
                api.logout()
            except Exception:
                pass

    watchlist: Dict[str, dict] = {}
    for code in codes:
        df_1m = classify_buy_sell_vol(stock_1m_map[code])
        result = filter_after_market(
            df_1m,
            index_df_daily=index_df,
            big_holder_df=bh_df_map.get(code),
            institutional_df=inst_df_map.get(code),
        )
        result["code"] = code
        watchlist[code] = result

    qualified = [c for c, r in watchlist.items() if r["final_signal"]]

    # 寫 mls.db (live 模式且有合格才寫)
    if not dry_run and qualified:
        written = write_watchlist_to_mls_db(watchlist, reason_prefix="TSS_v1")
    else:
        written = 0

    return {
        "ts": datetime.now().isoformat(),
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "watchlist": watchlist,
        "qualified": qualified,
        "watchlist_count": len(watchlist),
        "qualified_count": len(qualified),
        "written_to_db": written,
        "dry_run": dry_run,
        "status": "ok",
    }


# ── 背景排程 (lifespan 啟動時跑一次,後續每 60 分鐘跑一次,簡化版) ──
_scheduler_thread: Optional[threading.Thread] = None
_scheduler_stop = threading.Event()


def _scheduler_loop():
    """每日 13:40 (Asia/Taipei) 觸發一次盤後篩選 (簡化版,只在啟動時跑一次 + 每小時檢查)。"""
    last_run_date = None
    while not _scheduler_stop.is_set():
        now = datetime.now()
        # 簡化:每天 13:40 ~ 14:00 之間第一次跑到就跑 (避免重複)
        if now.hour == 13 and 40 <= now.minute <= 59 and now.strftime("%Y-%m-%d") != last_run_date:
            try:
                result = _do_filter(dry_run=False, days=30)
                _LAST_RESULT.update(result)
                last_run_date = now.strftime("%Y-%m-%d")
                print(f"[tss_api] 背景篩選完成: 合格 {result['qualified_count']} 檔")
            except Exception as e:
                print(f"[tss_api] 背景篩選失敗: {e}")
        _scheduler_stop.wait(timeout=60)  # 每分鐘檢查一次


@asynccontextmanager
async def lifespan(app: FastAPI):
    """啟動時跑背景 scheduler_loop。"""
    global _scheduler_thread
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="tss-scheduler")
    _scheduler_thread.start()
    print("[tss_api] 背景 scheduler 啟動")
    yield
    _scheduler_stop.set()


app = FastAPI(
    title="TSS v1.0 API",
    description="台股主動買賣盤四因子進場策略 — 獨立 API (port 8765)",
    version="1.0",
    lifespan=lifespan,
)


@app.get("/api/tss/health")
def health():
    """健康檢查。"""
    return {
        "status": "ok",
        "ts": datetime.now().isoformat(),
        "scheduler_alive": _scheduler_thread is not None and _scheduler_thread.is_alive(),
        "last_run_ts": _LAST_RESULT.get("ts"),
        "last_qualified_count": _LAST_RESULT.get("qualified_count", 0),
    }


@app.get("/api/tss/watchlist")
def get_watchlist(trade_date: Optional[str] = None):
    """
    從 mls.db 讀 watchlist。
    跟「盤後資金健康度」共用同一張表 (會看到雙方寫入)。
    """
    df = read_watchlist_from_mls_db(trade_date)
    if df.empty:
        return JSONResponse(
            {"trade_date": trade_date or datetime.now().strftime("%Y-%m-%d"),
             "rows": [], "count": 0, "source": "mls.db"},
        )
    return JSONResponse({
        "trade_date": trade_date or datetime.now().strftime("%Y-%m-%d"),
        "rows": df.to_dict("records"),
        "count": len(df),
        "source": "mls.db",
    })


@app.post("/api/tss/run")
def run_filter(days: int = 30, dry_run: bool = False, background: bool = True):
    """
    觸發盤後篩選。
    - background=True (default): 在背景 thread 跑,API 立刻回 {status: started}
    - background=False: 同步跑完回結果 (耗時 1-3 分鐘)
    """
    if background:
        def _bg():
            try:
                result = _do_filter(dry_run=dry_run, days=days)
                _LAST_RESULT.update(result)
            except Exception as e:
                _LAST_RESULT.update({"status": "error", "error": str(e), "ts": datetime.now().isoformat()})

        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        return {"status": "started", "message": "篩選在背景執行,稍後查 /api/tss/last"}

    result = _do_filter(dry_run=dry_run, days=days)
    _LAST_RESULT.update(result)
    return {
        "status": "ok",
        "trade_date": result["trade_date"],
        "qualified_count": result["qualified_count"],
        "qualified": result["qualified"],
        "written_to_db": result["written_to_db"],
    }


@app.get("/api/tss/last")
def get_last():
    """看最近一次篩選結果 (記憶體快取)。"""
    if _LAST_RESULT.get("ts") is None:
        return {"status": "never_run", "message": "尚未跑過,請 POST /api/tss/run"}
    return {
        "status": _LAST_RESULT.get("status", "unknown"),
        "ts": _LAST_RESULT.get("ts"),
        "trade_date": _LAST_RESULT.get("trade_date"),
        "qualified_count": _LAST_RESULT.get("qualified_count", 0),
        "qualified": _LAST_RESULT.get("qualified", []),
        "watchlist_count": _LAST_RESULT.get("watchlist_count", 0),
        "written_to_db": _LAST_RESULT.get("written_to_db", 0),
        "dry_run": _LAST_RESULT.get("dry_run", False),
    }


@app.get("/api/tss/watchlist/{code}")
def get_stock_detail(code: str, days: int = 30):
    """看單檔的篩選細節 (從最近一次結果讀)。"""
    if _LAST_RESULT.get("ts") is None:
        raise HTTPException(404, "尚未跑過篩選,請先 POST /api/tss/run")
    wl = _LAST_RESULT.get("watchlist", {})
    if code not in wl:
        raise HTTPException(404, f"{code} 不在最近一次篩選結果中 (共 {len(wl)} 檔)")
    return wl[code]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)