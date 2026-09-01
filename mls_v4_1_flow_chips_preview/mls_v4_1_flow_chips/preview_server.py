"""Standalone FastAPI preview server for MLS v4.1 Flow × Chips.

Run locally only in this phase:
    python -m mls_v4_1_flow_chips.preview_server

It does not import the existing MLS server and does not register routes on it.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .repository import apply_schema, open_db
from .service import build_top10, build_reversal_day1

TW_TZ = timezone(timedelta(hours=8))
PACKAGE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = PACKAGE_DIR / "schema.sql"
HTML_PATH = PACKAGE_DIR / "flow_chips.html"
DEFAULT_DB = PACKAGE_DIR.parent / "preview.db"


def _today() -> str:
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


def create_app(db_path: str | Path = DEFAULT_DB) -> FastAPI:
    db_path = Path(db_path)
    with open_db(db_path) as conn:
        apply_schema(conn, SCHEMA_PATH)

    app = FastAPI(title="MLS v4.1 Flow × Chips Preview")
    app.state.db_path = db_path

    @app.get("/api/flow-chips/health")
    def health():
        with open_db(app.state.db_path) as conn:
            counts = {}
            for table in ("intraday_snapshot", "chip_daily", "decision_history", "reversal_day1_history"):
                counts[table] = conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
        return {"mode": "isolated-preview", "db": str(app.state.db_path), "counts": counts}

    @app.get("/api/flow-chips/top10")
    def top10(
        direction: str = Query("inflow", pattern="^(inflow|outflow)$"),
        trade_date: str | None = Query(None, description="YYYY-MM-DD"),
    ):
        date = trade_date or _today()
        with open_db(app.state.db_path) as conn:
            return JSONResponse(build_top10(conn, date, direction))

    @app.get("/api/flow-chips/reversal-day1")
    def reversal_day1(
        trade_date: str | None = Query(None, description="YYYY-MM-DD"),
    ):
        date = trade_date or _today()
        with open_db(app.state.db_path) as conn:
            return JSONResponse(build_reversal_day1(conn, date))

    @app.get("/")
    def page():
        return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))

    return app


app = create_app(os.environ.get("MLS_FLOW_CHIPS_DB", str(DEFAULT_DB)))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8011")))
