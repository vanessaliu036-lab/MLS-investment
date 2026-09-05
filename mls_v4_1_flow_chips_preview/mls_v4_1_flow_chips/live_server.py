"""Live-only Reversal Lab server for the isolated VPS sidecar."""
from __future__ import annotations

import html
import json
import os
import asyncio
import logging
from pathlib import Path
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone
import threading

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from .live_bridge import build_live_view, fetch_live_rows
from .live_run import render_html, render_history_html
from .reversal_state import ReversalStateMachine


SOURCE_URL = os.environ.get(
    "MLS_REVERSAL_SOURCE_URL",
    "http://127.0.0.1:8000/api/intraday-watchpool",
)
FETCH_TIMEOUT = int(os.environ.get("MLS_REVERSAL_FETCH_TIMEOUT", "20"))
LINE_B_URL = os.environ.get(
    "MLS_REVERSAL_LINE_B_URL",
    "http://127.0.0.1:8000/line-b-ledger.json",
)
COLLECT_INTERVAL_SECONDS = max(30, int(os.environ.get("MLS_REVERSAL_COLLECT_INTERVAL", "60")))
STATE_MACHINE = ReversalStateMachine()
LOGGER = logging.getLogger("mls-reversal-lab")
TW_TZ = timezone(timedelta(hours=8))
HISTORY_FILE = Path(os.environ.get(
    "MLS_REVERSAL_HISTORY_FILE",
    str(Path(__file__).resolve().parents[1] / "reversal_history.json"),
))
HISTORY_LOCK = threading.RLock()


def _history_read() -> dict:
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("days"), dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "days": {}}


def _history_write(data: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix(HISTORY_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(HISTORY_FILE)


def _history_date(view: dict) -> str:
    raw = view.get("updated_at") or view.get("snapshot")
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TW_TZ)
        return parsed.astimezone(TW_TZ).date().isoformat()
    except (TypeError, ValueError):
        return datetime.now(TW_TZ).date().isoformat()


def _record_reversal_history(view: dict) -> dict:
    """Keep one latest top-20 snapshot per trading date; never clear old dates."""
    trade_date = _history_date(view)
    with HISTORY_LOCK:
        data = _history_read()
        old = data["days"].get(trade_date) or {}
        data["days"][trade_date] = {
            "trade_date": trade_date,
            "first_saved_at": old.get("first_saved_at") or datetime.now(TW_TZ).isoformat(timespec="seconds"),
            "updated_at": view.get("updated_at") or view.get("snapshot"),
            "rows": list(view.get("reversal") or [])[:20],
        }
        _history_write(data)
        return data


def _history_payload(date: str = "") -> dict:
    with HISTORY_LOCK:
        data = _history_read()
    dates = sorted(data.get("days", {}), reverse=True)
    selected = date if date in data.get("days", {}) else (dates[0] if dates else "")
    record = data.get("days", {}).get(selected) or {}
    return {
        "ok": True,
        "dates": dates,
        "pool_date": selected,
        "trade_date": selected,
        "updated_at": record.get("updated_at"),
        "rows": list(record.get("rows") or [])[:20],
        "count": len(record.get("rows") or []),
    }


def _load_line_b_context() -> dict:
    """Read the existing C1+C2 ledger; failure never fabricates a classification."""
    try:
        req = Request(LINE_B_URL, headers={"User-Agent": "MLS-Reversal-Lab/1.0"})
        with urlopen(req, timeout=FETCH_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"has_data": False, "error": str(exc)}


def _load_view() -> dict:
    payload = fetch_live_rows(SOURCE_URL, timeout=FETCH_TIMEOUT)
    view = build_live_view(
        payload,
        line_b_payload=_load_line_b_context(),
        state_machine=STATE_MACHINE,
    )
    history = _record_reversal_history(view)
    view["reversal_history_dates"] = sorted(history.get("days", {}), reverse=True)
    return view


def _error_page(exc: Exception) -> str:
    message = html.escape(str(exc))
    return f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Reversal Lab 資料錯誤</title>
    <style>body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:680px;margin:12vh auto;padding:24px;color:#172033}}.box{{border:1px solid #efd888;background:#fff8df;border-radius:16px;padding:20px}}h1{{font-size:22px}}p{{line-height:1.6}}</style>
    </head><body><div class='box'><h1>資金反轉驗證 / Reversal Lab</h1>
    <p>VPS 8000 真實資料目前無法取得，頁面沒有補造任何數字。</p><p>{message}</p>
    <p><button onclick='location.reload()'>重新載入</button></p></div></body></html>"""


def create_app() -> FastAPI:
    app = FastAPI(title="MLS Reversal Lab live sidecar")

    @app.on_event("startup")
    async def start_collector() -> None:
        async def collect() -> None:
            while True:
                try:
                    await asyncio.to_thread(_load_view)
                except Exception as exc:
                    LOGGER.warning("reversal state collection failed: %s", exc)
                await asyncio.sleep(COLLECT_INTERVAL_SECONDS)

        app.state.collector_task = asyncio.create_task(collect())

    @app.on_event("shutdown")
    async def stop_collector() -> None:
        task = getattr(app.state, "collector_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @app.get("/api/reversal-lab/health")
    def health():
        try:
            view = _load_view()
            return {
                "ok": True,
                "mode": "live-readonly",
                "source_url": SOURCE_URL,
                "updated_at": view.get("updated_at"),
                "snapshot": view.get("snapshot"),
                "inflow_count": len(view.get("inflow", [])),
                "outflow_count": len(view.get("outflow", [])),
                "reversal_count": len(view.get("reversal", [])),
            }
        except Exception as exc:
            return JSONResponse({"ok": False, "mode": "live-readonly", "error": str(exc)}, status_code=502)

    @app.get("/api/reversal-lab")
    def data():
        try:
            return JSONResponse(_load_view(), headers={"Cache-Control": "no-store"})
        except Exception as exc:
            return JSONResponse({"ok": False, "mode": "live-readonly", "error": str(exc)}, status_code=502)

    @app.get("/api/reversal-lab/history")
    def history(date: str = ""):
        return JSONResponse(_history_payload(date), headers={"Cache-Control": "no-store"})

    @app.get("/history", response_class=HTMLResponse)
    def history_page(date: str = ""):
        return HTMLResponse(render_history_html(_history_payload(date)), headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/", response_class=HTMLResponse)
    def page():
        try:
            return HTMLResponse(render_html(_load_view()), headers={"Cache-Control": "no-store, max-age=0"})
        except Exception as exc:
            return HTMLResponse(_error_page(exc), status_code=502, headers={"Cache-Control": "no-store, max-age=0"})

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8011")))
