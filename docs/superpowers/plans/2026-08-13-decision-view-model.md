# Decision View Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify model classification, entry timing, ranking, explanatory reasons, chip labels, and mobile grouping without changing the four-gate rejection rule.

**Architecture:** Add a pure `decision_view.py` module under the canonical AB engine and make all pipelines call it. Persist canonical fields in candidate payloads so the 8000 UI only renders backend decisions. Keep legacy score fields for compatibility, but remove financing from reason arrays.

**Tech Stack:** Python 3, unittest, FastAPI JSON payloads, vanilla HTML/CSS/JavaScript, systemd.

## Global Constraints

- Four structural-failure gates remain unchanged.
- Margin financing is never an entry reason or upgrade condition.
- Every non-rejected stock belongs to exactly one visible classification pool.
- Potential and entry timing remain separate.
- Frontend never reclassifies a backend row.

---

### Task 1: Pure Decision View Model

**Files:**
- Create: `篩選邏輯/decision_view.py`
- Create: `篩選邏輯/tests/test_decision_view.py`

**Interfaces:**
- `build(classified, market, trigger) -> dict`
- `ranking_factors(market) -> dict`
- `margin_tag(margin) -> str`

- [ ] Write tests for pool mapping, confirmed reversal upgrade, six entry states,
  margin exclusion, chip tags, reason tags, and three ranking factors.
- [ ] Run the tests and verify expected failures because the module is missing.
- [ ] Implement the pure model and make its tests pass.

### Task 2: Pipeline Integration

**Files:**
- Modify: `篩選邏輯/screen_post.py`
- Modify: `篩選邏輯/screen_intraday.py`
- Modify: `篩選邏輯/funnel.py`
- Modify: `篩選邏輯/api.py`
- Test: `篩選邏輯/tests/test_pipeline_retention.py`

- [ ] Add failing integration/source-contract tests proving all three pipelines
  call `decision_view`, `reasons` has no financing text, and history/tomorrow APIs
  expose `chip_tags` and `next_upgrade_condition`.
- [ ] Integrate the canonical view and preserve old payload compatibility.
- [ ] Sort inside each classification pool by `decision_rank_score`, not streak.
- [ ] Run the full AB test suite.

### Task 3: Mobile Decision UI

**Files:**
- Modify: `intraday_decision_dataflow.html`
- Modify: `篩選邏輯/tests/test_frontend_limit_rows.py`

- [ ] Add failing HTML contract tests for four expandable pool summaries, no
  uniform source column, status-machine copy, short reason tags, upgrade
  condition, and compact institution/flow/margin/volume metrics.
- [ ] Render canonical fields and preserve exact-limit-up red rows.
- [ ] Run frontend contract tests and inspect the served HTML.

### Task 4: Deployment and Production Verification

**Files:**
- Deploy with: `deploy_screen_vps.sh`
- Selectively sync 8000 HTML to `/opt/mls-intraday`

- [ ] Deploy 8002 and wait for health.
- [ ] Deploy the 8000 HTML and restart the main service.
- [ ] Verify services, API payload invariants, mobile HTML markers, T+1 dropped
  flow, and source-manifest hashes.
- [ ] Commit only files belonging to this change.
