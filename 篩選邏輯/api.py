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

from fastapi import Body, FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse

import config
import screen_intraday
import screen_post
import store
from phase import Phase, get_phase, describe, today_tw

# B 鏈(盤中發現 → 盤後驗證 → 隔日候選)。與 A 鏈完全獨立,只在 merge_pool 交會。
import b_snapshot
import b_discover
import b_verify
import merge_pool
# 當日收盤復盤(命中率/勝率分析層)。衡量模型準度,與名單產生脫鉤。
import screen_verify

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
        # 盤中嚴判:只盯昨日候選池,四條件全中才綠燈(不吃 universe,自己讀 pool)
        data = screen_intraday.build()
    else:
        data = screen_post.build(config.UNIVERSE)

    return JSONResponse(data)


# ════════════════════════════════════════════════════════════════════
# B 鏈端點(UI 分開:盤中畫面永遠只有 A 鏈燈號;B 鏈的東西收盤後才出現)
# 盤中發現 → 盤後法人驗證 → 匯流成明日候選池新血。
# ════════════════════════════════════════════════════════════════════

@app.post("/api/b/snapshot")
def b_take_snapshot(buffer: dict = Body(..., description="Shioaji 記憶體 buffer {code:{price,change_rate,volume,net_active,bid_vol,ask_vol}}")):
    """盤中每 5 分鐘由訂閱端推一次。零 API、純本地寫檔。非盤中時段自動略過。"""
    return JSONResponse(b_snapshot.take(buffer))


@app.post("/api/b/scan")
def b_scan():
    """13:20 最終掃描:四判準中兩項成立才標記。不出燈號、不進盤中畫面。"""
    return JSONResponse(b_discover.scan(config.UNIVERSE, config.CODE_GROUP))


@app.get("/api/b/discovery")
def b_discovery_view():
    """B 鏈今日發現(唯讀顯示)。盤中只記錄,收盤後才有意義。"""
    rows = store.read_date("b_discovery", today_tw())
    items = sorted(rows.values(), key=lambda r: (-(r.get("hits") or 0), r["code"]))
    return JSONResponse({
        "chain": "B", "data_date": today_tw().isoformat(),
        "purpose": f"B鏈盤中發現 {len(items)} 檔 — 非進場訊號,待盤後法人驗證",
        "actionable": False, "items": items,
    })


@app.post("/api/b/verify")
def b_do_verify():
    """13:31 後:用今日法人數字驗 B 鏈標記(PASS 進池 / FAIL 淘汰 / NO_DATA 留驗)。"""
    return JSONResponse(b_verify.verify())


@app.post("/api/pool/merge")
def pool_merge():
    """盤後匯流:明日候選池 = A鏈寬篩 ∪ B鏈驗證通過。任一鏈掛掉另一照併。"""
    return JSONResponse(merge_pool.merge())


@app.get("/api/pool/tomorrow")
def pool_tomorrow():
    """隔日盤中觀察清單(唯讀):匯流後的候選池,標源(A鏈/雙鏈確認/B鏈新血)。"""
    rows = store.read_date("candidate_pool", today_tw())
    items = sorted(rows.values(), key=lambda r: (r.get("rank") or 999, r["code"]))
    return JSONResponse({
        "data_date": today_tw().isoformat(),
        "purpose": f"明日盤中候選池 {len(items)} 檔 — 明天只盯這些,非進場名單",
        "actionable": False, "items": items,
    })


# ════════════════════════════════════════════════════════════════════
# 當日收盤復盤(命中率/勝率)— 衡量模型準度,獨立於名單產生
# ════════════════════════════════════════════════════════════════════

@app.post("/api/verify/run")
def verify_run():
    """用今日收盤復盤昨日候選池,依進場軌判命中,寫 pool_outcome。"""
    return JSONResponse(screen_verify.verify())


@app.get("/api/verify/stats")
def verify_stats(days: int = Query(30, description="滾動交易日窗")):
    """滾動勝率/報酬(分軌)。模型的真正驗證。"""
    return JSONResponse(screen_verify.stats(days))


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
