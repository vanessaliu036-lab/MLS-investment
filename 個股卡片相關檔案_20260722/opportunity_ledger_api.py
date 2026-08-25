"""Opportunity Ledger 頁面路由——唯讀,不計分。

⚠ 8000 站(個股卡片相關檔案_20260722/)與 8002 引擎(篩選邏輯/)是分開部署
的兩個目錄(見 deploy_vps.sh:8000 站推 /opt/mls-intraday,篩選邏輯推
/opt/mls-screen,且互相 --exclude)。這支檔案住在 8000 站這邊,但實際
渲染邏輯(`opportunity_ledger_view.py` / `opportunity_ledger_render.py`)
跟計分邏輯(`opportunity_score.py`)一起放在 篩選邏輯/,因為它們是同一組
概念、同一份測試在管——這裡只做「找得到就 import,找不到就不掛路由」,
不複製一份程式碼出來維護兩份。

資料來源固定是 production `opportunity_snapshot`(/opt/mls-screen/mls.db),
本檔跟它的兩個依賴一樣,只 SELECT,不重算 score/tier/evidence。
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

router = None

_HERE = Path(__file__).resolve().parent

_SCREEN_DIR_CANDIDATES = [
    os.environ.get("MLS_SCREEN_DIR"),
    "/opt/mls-screen",                          # VPS production(8002 引擎正本)
    str(_HERE.parent / "篩選邏輯"),              # 本機開發(同一份 repo 底下)
]
for _c in _SCREEN_DIR_CANDIDATES:
    if _c and Path(_c).is_dir() and _c not in sys.path:
        sys.path.insert(0, _c)
        break

_DB_CANDIDATES = [
    os.environ.get("MLS_OPPORTUNITY_DB_PATH"),
    "/opt/mls-screen/mls.db",
    str(_HERE.parent / "篩選邏輯" / "mls.db"),
]
DB_PATH = next((p for p in _DB_CANDIDATES if p and Path(p).exists()), _DB_CANDIDATES[-1])

try:
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    import opportunity_ledger_view as _view
    import opportunity_ledger_render as _render

    router = APIRouter()

    @router.get("/opportunity-ledger")
    def opportunity_ledger_page():
        """機會分層觀察榜——族群訊號 REPLICATED,個股層 DESCRIPTIVE ONLY,
        唯讀讀取 production opportunity_snapshot,不在這裡重算任何分層。"""
        ctx = _view.build_ledger_context(DB_PATH)
        html = _render.render_ledger_html(ctx)
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})

except Exception as _exc:  # 依賴或路徑找不到時不得讓整站掛掉
    print(f"[opportunity-ledger] router 建置失敗,頁面停用:{_exc}")
    router = None
