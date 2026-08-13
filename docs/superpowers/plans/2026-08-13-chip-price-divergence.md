# Chip-Price Divergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an auditable five-type chip-price divergence scanner that changes priority/classification but never independently rejects a stock.

**Architecture:** A pure `chip_price_divergence.py` consumes newest-first dated rows and emits one mutually exclusive result. `screen_post` gathers dated facts and passes the result to the central `decision_view`; APIs and UI only display canonical fields.

**Tech Stack:** Python 3.9, SQLite, unittest, vanilla JavaScript/HTML, FastAPI.

## Global Constraints

- Divergence may upgrade, downgrade, prioritize, or block chasing; it must never bypass the four structural-failure gates.
- Institutional flow, active flow, and ownership are distinct facts and must not be relabeled as one another.
- Missing ownership data is pending, never inferred from net buying.
- Five types are mutually exclusive in priority order C > A > B > D > E.

---

### Task 1: Pure Divergence Scanner

**Files:**
- Create: `篩選邏輯/chip_price_divergence.py`
- Create: `篩選邏輯/tests/test_chip_price_divergence.py`

**Interfaces:**
- Consumes: newest-first `inst_rows`, `bar_rows`, `aflow_rows`.
- Produces: `scan(inst_rows, bar_rows, aflow_rows) -> dict` using fields from the design spec.

- [ ] Write failing tests for A, B, C, D, E, missing data, mutual exclusion, and ownership pending.
- [ ] Run the focused tests and confirm failures are caused by the missing module/behavior.
- [ ] Implement normalized metrics and the C > A > B > D > E decision chain.
- [ ] Run focused tests and confirm all pass.
- [ ] Commit the scanner and tests.

### Task 2: Central Decision Integration

**Files:**
- Modify: `篩選邏輯/decision_view.py`
- Modify: `篩選邏輯/screen_post.py`
- Modify: `篩選邏輯/funnel.py`
- Modify: `篩選邏輯/screen_intraday.py`
- Modify: `篩選邏輯/tests/test_decision_view.py`
- Modify: `篩選邏輯/tests/test_pipeline_retention.py`

**Interfaces:**
- Consumes: scanner result through optional `divergence` argument.
- Produces: canonical `divergence_*` fields plus adjusted grade, priority, entry state, tags, and rank.

- [ ] Write failing tests proving C upgrades, A/B prioritize, D blocks chasing, E cannot reject, and all pipelines retain scanner fields.
- [ ] Run focused tests and observe the expected failures.
- [ ] Read up to 20 dated rows in `screen_post`, persist scanner output, and pass it to Decision View.
- [ ] Apply only the integration matrix from the design spec; preserve structural rejection.
- [ ] Recompute the scanner during intraday/read-time adaptation from persisted dated metrics where available.
- [ ] Run focused and full backend tests.
- [ ] Commit central integration.

### Task 3: API and Mobile UI

**Files:**
- Modify: `篩選邏輯/api.py`
- Modify: `intraday_decision_dataflow.html`
- Modify: `篩選邏輯/tests/test_frontend_limit_rows.py`

**Interfaces:**
- Consumes: canonical `divergence_label`, `divergence_reasons`, `divergence_metrics`, `divergence_pending`.
- Produces: one compact divergence badge and up to three fact tags on every stock card.

- [ ] Write failing API/static UI contract tests for all canonical fields and five labels.
- [ ] Run tests and observe expected failures.
- [ ] Forward fields through tomorrow/history APIs and render the badge without frontend classification logic.
- [ ] Run tests and parse every inline script with Node.
- [ ] Commit API/UI integration.

### Task 4: Deploy and Verify

**Files:**
- Deploy: `篩選邏輯/` to `/opt/mls-screen` (8002).
- Deploy: `intraday_decision_dataflow.html` to `/opt/mls-intraday` (8000).

- [ ] Run all local tests and `git diff --check`.
- [ ] Deploy 8002 through `deploy_screen_vps.sh` and wait for health.
- [ ] Selectively sync the main-site HTML and restart 8000.
- [ ] Assert online payload fields, mutual exclusion, unchanged rejection behavior, and sample diagnostics.
- [ ] Verify both systemd services and the 8002 drift guard.
