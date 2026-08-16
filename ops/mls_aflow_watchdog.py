#!/usr/bin/env python3
"""Restart mls-intraday after two consecutive dead aflow checks.

Runs from a systemd timer.  It only acts during Taiwan market hours and never
manufactures fallback flow.  A state file provides debounce and restart
cooldown so a transient HTTP failure cannot create a restart loop.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
import urllib.request
from pathlib import Path

TW = dt.timezone(dt.timedelta(hours=8))
STATE = Path("/run/mls-aflow-watchdog.json")
URL = "http://127.0.0.1:8000/api/intraday-test"
FAILS_TO_RESTART = 2
RESTART_COOLDOWN = 300


def market_open(now: dt.datetime) -> bool:
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and 9 * 60 <= minute <= 13 * 60 + 30


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"fails": 0, "last_restart": 0}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state))


def live_count() -> tuple[int, int]:
    with urllib.request.urlopen(URL, timeout=25) as response:
        payload = json.loads(response.read().decode())
    rows = payload.get("rows") or []
    return sum(r.get("aflow_status") == "LIVE" and r.get("aflow") is not None
               for r in rows), len(rows)


def main() -> int:
    now = dt.datetime.now(TW)
    state = load_state()
    if not market_open(now):
        state["fails"] = 0
        save_state(state)
        return 0

    try:
        live, total = live_count()
        healthy = total > 0 and live >= max(1, total // 2)
    except Exception:
        live, total, healthy = 0, 0, False

    if healthy:
        state["fails"] = 0
        save_state(state)
        print(f"healthy live={live}/{total}")
        return 0

    state["fails"] = int(state.get("fails", 0)) + 1
    elapsed = time.time() - float(state.get("last_restart", 0))
    if state["fails"] >= FAILS_TO_RESTART and elapsed >= RESTART_COOLDOWN:
        print(f"dead live={live}/{total}; restarting mls-intraday")
        state["fails"] = 0
        state["last_restart"] = time.time()
        save_state(state)
        subprocess.run(["systemctl", "restart", "mls-intraday.service"], check=True)
    else:
        save_state(state)
        print(f"degraded live={live}/{total}; fail={state['fails']} cooldown={int(elapsed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
