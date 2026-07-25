"""
api.py — 唯一的名單出口

規範:
1. 名單只能在後端算,只能有兩支計分函式(盤中一支、盤後一支)。
2. 前端不准出現任何 filter、任何 if 判定分組、任何自訂排序。
   前端只准做兩件事:選欄位顯示、取前 N 筆。
3. 只有一個名單端點 /api/watchlist。
   舊的 /api/intraday-test、/api/watchpool、機會雷達各自的端點全部廢除。
4. purpose 那行字由後端決定,前端不准自己寫。

驗收條件:任兩個分頁同時打開,前 10 檔的股票代號和順序必須完全一致。
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse

import config
import screen_intraday
import screen_post
import store
from phase import Phase, get_phase, describe

app = FastAPI(title="MLS v4.0")


@app.on_event("startup")
def _startup():
    """
    開機只做兩件事:建表、跑自檢。
    不跑任何補資料流程、不重抓、不重算。缺哪天的資料標「缺 X/XX」,你自己按按鈕補。
    這是你等待的主因,現在移除。
    """
    store.init_db()
    import preflight
    preflight.run(fail_fast=True)


@app.get("/api/watchlist")
def watchlist(phase: Optional[str] = Query(None, description="PRE|INTRADAY|POST,預設依當下時段")):
    """
    全系統唯一的名單端點。每檔已經算完 score / rank / reasons / missing。

    固定回傳 config.UNIVERSE 全集,不預先剔除。
    沒回報的檔標 has_data=false,灰掉但不消失 —— 消失比顯示更誤導。
    """
    ph = Phase(phase) if phase else get_phase()

    if ph in (Phase.PRE, Phase.CLOSED):
        # 盤前/休市 = 直接讀上一交易日盤後名單,不重算、不重抓、零 API,秒開。
        # 休市(週末/國定假日)絕不因為時鐘到 09:00 就跑盤中。
        data = screen_post.load_for_premarket()
        if ph is Phase.CLOSED:
            info = describe(Phase.CLOSED)
            data["phase"] = info["phase"]
            data["purpose"] = info["purpose"]
            data["actionable"] = False
    elif ph is Phase.INTRADAY:
        data = screen_intraday.build(config.UNIVERSE)
    else:
        data = screen_post.build(config.UNIVERSE)

    return JSONResponse(data)


@app.get("/api/phase")
def phase_info():
    return describe()


@app.get("/api/health")
def health():
    """今天實際打了幾次外部 API。重開服務後應該是 0。"""
    return {
        "fetch_count_today": store.fetch_count_today(),
        "phase": get_phase().value,
    }


@app.get("/")
def index():
    return FileResponse("index.html")
