# MLS Continuous Scoring Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the old 60-point floor with the supplied continuous scoring engine and expose its score, grade, stars, risks, and dimension breakdown through MLS.

**Architecture:** Keep the four-layer funnel as the qualification workflow. Add the supplied scoring engine as a pure scoring module, adapt the existing EOD snapshot/chip fields into `StockInput`, and persist the resulting score/grade/stars in the existing decision record. The UI/API keeps the existing response shape while adding scoring details.

**Tech Stack:** Python 3, dataclasses, SQLite, FastAPI, unittest, Docker Compose.

## Global Constraints

- EOD data only for institutional and margin fields.
- `NO_DATA` never counts as PASS and large-holder custody remains `NO_DATA`.
- `vs_sector` affects score/order only and never becomes a qualification veto.
- Livermore remains a play-style classifier, not a selection gate.

### Task 1: Add and test the pure scoring engine

**Files:**
- Create: `app/scoring.py`
- Create: `tests/test_scoring.py`

- [ ] Copy the supplied `StockInput`, dimension functions, risk handling, and `compute_health_score` into `app/scoring.py` without changing the documented weights.
- [ ] Port the supplied fixture cases into `tests/test_scoring.py` and assert score spread, 2481 > 1815, NO_DATA behavior, and hard-risk capping.
- [ ] Run `python3 -m unittest tests.test_scoring -v`; expected: all tests pass.

### Task 2: Wire scoring into the formal decision path

**Files:**
- Modify: `app/decision.py`
- Modify: `app/chips.py`
- Modify: `app/db.py`
- Modify: `tests/test_scoring.py`

- [ ] Build `StockInput` inside `evaluate_stock()` from snapshot/chip/sector values, mapping missing custody to `NO_DATA` and deriving absorption states from the existing four-state logic.
- [ ] Replace the old score/grade/stars result with `compute_health_score()` while retaining existing fields used by the funnel, reports, and trade plan.
- [ ] Persist the scoring dimension JSON and risk arrays in SQLite-compatible fields or response fields without breaking existing databases.
- [ ] Run the full unit suite and a temporary demo EOD against a temporary SQLite database; expected: 51 stocks produce a non-zero score range.

### Task 3: Align API and frontend data

**Files:**
- Modify: `app/main.py`
- Modify: `app/web/index.html`

- [ ] Ensure `/api/v2/today` and `/api/eod` expose the continuous score and its grade consistently.
- [ ] Display dimension breakdown and `Ready候選` alongside Ready/Watch.
- [ ] Keep L1-L4 funnel status separate from score so scoring does not accidentally become a gate.
- [ ] Verify homepage and JSON endpoints locally.

### Task 4: Deploy and verify

**Files:**
- Modify: `Dockerfile` only if dependency/runtime changes are required.

- [ ] Upload changed application files to `/opt/mls-v4-new` on the configured server.
- [ ] Rebuild the `mls-v4` container on port 8003 without stopping the existing port 8000 service.
- [ ] Verify `/api/health`, `/api/eod`, `/api/funnel`, and `/` return successfully and report a non-zero score range.
