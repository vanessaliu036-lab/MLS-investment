# MLS Five-State Core Classifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify `screen_post` and `funnel` behind one five-state classifier that only rejects when all four structural failure gates pass, measure false negatives at T+1 +5%/+9%, and render true limit-up rows with a Taiwan-market red background.

**Architecture:** Extend `篩選邏輯/layered_score.py` into the pure central classification boundary. Both pipelines normalize their daily, intraday, active-flow and institution data into `build_input()` and consume `score_layered()`; legacy funnel layers remain diagnostic features only. The static UI consumes a shared limit-up predicate and row class.

**Tech Stack:** Python 3, `unittest`, SQLite, vanilla HTML/CSS/JavaScript, FastAPI, systemd.

## Global Constraints

- Only all four gates together may return `❌ 結構失效`.
- Missing data or one to three gates must never reject.
- Institution streaks are features, never hard gates.
- Potential and entry status are separate output fields.
- Categories are exactly the approved five labels.
- T+1 false-negative KPI thresholds are exactly 5% and 9%.
- Preserve unrelated dirty-worktree changes.

---

### Task 1: Central classifier contract

**Files:**
- Modify: `篩選邏輯/layered_score.py`
- Test: `篩選邏輯/tests/test_core_classifier.py`

**Interfaces:**
- Consumes: `build_input(code, bar, inst, previous_bar, aflow_today, aflow_previous, series, ...)`.
- Produces: `score_layered(features)` with `classification`, `failure_gates`, `turn_signals`, `potential_grade`, `trend_stage`, and `entry_status`.

- [ ] Write table-driven tests proving zero through three gates never reject and all four gates reject.
- [ ] Run `python3 -m unittest 篩選邏輯/tests/test_core_classifier.py -v` and observe failures against the existing two-category structural gate.
- [ ] Implement normalized fields, four gate evaluation, turn signals, lifecycle and exact five-state classification.
- [ ] Rerun the test and require all cases to pass.

### Task 2: Pipeline unification and institution features

**Files:**
- Modify: `篩選邏輯/screen_post.py`
- Modify: `篩選邏輯/funnel.py`
- Test: `篩選邏輯/tests/test_pipeline_retention.py`

**Interfaces:**
- Consumes: central `score_layered()` output from Task 1.
- Produces: candidate/dropped rows where only `classification == ❌ 結構失效` is removed.

- [ ] Write tests proving foreign selling, institution selling streak, below-MA20, high bias, volume spike, upper shadow, sector lag and black candle each remain retained.
- [ ] Run the tests and observe current `funnel.layer1/layer25/layer3` hard-drop failures.
- [ ] Change legacy layers to diagnostics and make both pipeline final decisions consume the central classifier.
- [ ] Rerun classifier and pipeline tests and require all pass.

### Task 3: False-negative KPI

**Files:**
- Modify: `篩選邏輯/reject_verify.py`
- Test: `篩選邏輯/tests/test_reject_kpi.py`

**Interfaces:**
- Consumes: T close and T+1 OHLC for every dropped row.
- Produces: `fnr_5_rate`, `fnr_9_rate`, counts and target status without flow/tradability filters.

- [ ] Write fixtures where +5%/+9% outcomes count even with negative/missing flow and large opening gaps.
- [ ] Run tests and observe the old 4%/7% plus flow/relative-strength verdict fail.
- [ ] Implement direct 5%/9% outcome classification and aggregate fields.
- [ ] Rerun tests and require all pass.

### Task 4: Limit-up market styling

**Files:**
- Modify: `intraday_decision_dataflow.html`
- Test: browser smoke verification against the deployed page.

**Interfaces:**
- Consumes: `is_limit_up`, subgroup, and exact/compatible change-rate data.
- Produces: `limit-up-row` on homepage, watchlist, decision and dropped T+1 rows.

- [ ] Add a shared limit-up predicate and apply it to every stock-row renderer.
- [ ] Add high-contrast Taiwan-market red row CSS, including nested text and badges.
- [ ] Load production and verify 3532/6488/6182/2327/2492 rows render red while 8043/3026 do not unless their actual limit price is reached.

### Task 5: Deploy and verify

**Files:**
- Modify: `deploy_screen_vps.sh` only if the manifest lacks a changed 8002 file.

**Interfaces:**
- Consumes: local tested files.
- Produces: matching local/VPS hashes and healthy 8000/8002 endpoints.

- [ ] Run all Python tests and syntax checks locally.
- [ ] Deploy 8002 through the canonical sync script, restart `mls-ab-engine`, and verify `/health`, `/api/watchlist`, `/api/funnel`, `/api/verify/reject`.
- [ ] Deploy the HTML to 8000, restart `mls-intraday`, and verify the live page plus `/api/intraday-test`.
- [ ] Compare SHA-256 hashes and confirm 8/12 dropped rows are paired only with 8/13 T+1 flow.
- [ ] Report classifications, KPI fields, service health and any retained historical limitation.
