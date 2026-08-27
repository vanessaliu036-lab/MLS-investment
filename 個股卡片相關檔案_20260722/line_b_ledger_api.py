"""Line B Watch Ledger 頁面路由——唯讀,不計分,不改任何 production gate。

⚠ 同 opportunity_ledger_api.py 的分工:住在 8000 站這邊,但實際渲染邏輯
(line_b_ledger_view.py / line_b_ledger_render.py)跟收集邏輯
(run_line_b_ledger.py / line_b_watch_ledger.py)一起放在 篩選邏輯/,同一組
概念同一份測試在管,這裡只找得到就 import。

資料來源固定是 production `line_b_watch_ledger`(/opt/mls-screen/mls.db 的
新表,append-only,見 line_b_watch_ledger.py 的凍結定義與時序鎖)。

2026-08-27:已掛進 server.py app(見 server.py 的 include_router)。盤中
(phase.get_phase()==INTRADAY)且沒指定 date 時走 line_b_live 即時合成;其餘
情況(PRE/POST/CLOSED,或明確指定歷史 date)一律走 line_b_watch_ledger 已落地
的委員會 EOD 資料——只有這支模組會判斷要走哪條路徑,前端/server.py 不用管。
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Optional

router = None

_HERE = Path(__file__).resolve().parent

_SCREEN_DIR_CANDIDATES = [
    os.environ.get("MLS_SCREEN_DIR"),
    "/opt/mls-screen",
    str(_HERE.parent / "篩選邏輯"),
]
for _c in _SCREEN_DIR_CANDIDATES:
    if _c and Path(_c).is_dir() and _c not in sys.path:
        sys.path.insert(0, _c)
        break

_DB_CANDIDATES = [
    os.environ.get("MLS_LINE_B_DB_PATH"),
    "/opt/mls-screen/mls.db",
    str(_HERE.parent / "篩選邏輯" / "mls.db"),
]
DB_PATH = next((p for p in _DB_CANDIDATES if p and Path(p).exists()), _DB_CANDIDATES[-1])

try:
    from fastapi import APIRouter, Query
    from fastapi.responses import HTMLResponse, JSONResponse

    import phase as _phase
    import line_b_ledger_view as _view
    import line_b_ledger_render as _render
    import line_b_audit_log as _audit

    router = APIRouter()

    def _build_context(date: Optional[str]):
        """date 沒指定 且現在是盤中 → 即時合成(並 append-only 記錄本次顯示的
        機率);否則一律讀已落地的 EOD ledger(歷史 date 一定是這條路徑,不因為
        剛好是盤中就被誤導去查即時)。"""
        if date is None and _phase.get_phase() == _phase.Phase.INTRADAY:
            ctx = _view.build_live_context(DB_PATH)
            try:
                _audit.log_rows(ctx.get("data_date"), ctx.get("c1_c2_list", []) +
                                ctx.get("intraday_discovery", []), DB_PATH)
            except Exception as _log_exc:
                print(f"[line-b-ledger] audit log 寫入失敗(不影響頁面顯示):{_log_exc}")
            return ctx
        return _view.build_ledger_context(date, DB_PATH)

    @router.get("/line-b-ledger")
    def line_b_ledger_page(date: Optional[str] = Query(None)):
        """Line B Watch Mode 觀察 ledger —— research only,不是 production
        gate,不影響任何既有 tier/track/score。唯讀。"""
        ctx = _build_context(date)
        html = _render.render(ctx)
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})

    @router.get("/line-b-ledger.json")
    def line_b_ledger_json(date: Optional[str] = Query(None)):
        ctx = _build_context(date)
        return JSONResponse(ctx, headers={"Cache-Control": "no-store, max-age=0"})

except Exception as _exc:
    print(f"[line-b-ledger] router 建置失敗,頁面停用:{_exc}")
    router = None
