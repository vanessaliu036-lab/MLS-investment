# Early Activation Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an independent, auditable discovery pipeline that distinguishes `NEW_TURN`, `RECONFIRM`, and `ACCUMULATION_RETEST` without changing the frozen Opportunity Evidence pipeline.

**Architecture:** A pure classifier consumes one stock's T0 facts plus the prior five trading-day facts. A separate append-only `early_activation_snapshot` table stores classifications and T+1 close-to-close outcomes. A pure evaluator compares each setup with a same-date, same-sector-context no-setup baseline. No Early Activation field is imported by, written to, or used in `opportunity_snapshot` or Opportunity tiering.

**Tech Stack:** Python 3.9, SQLite, pytest, Markdown.

## Global Constraints

- Evidence status is always `DISCOVERY ONLY`; no score, probability, confidence, or buy signal is emitted.
- Opportunity Evidence remains frozen: `sec_rs_10d @ Top10%`, T+10/T+15, Net MFE ≥ +3%, and its tiering are untouched.
- Candidate facts are known as of T0 close. Outcome is T+1 close versus T0 close; it measures early identification, not executable trade return.
- Thresholds reuse existing frozen Pre-Activation constants: MA5 near ±2%, volume activation 1.2x, MA5 hot 7%, and foreign strong at two days.
- `RISK_OFF` is ineligible. `RISK_ON`, `TURNING_POSITIVE`, and `NEUTRAL` remain separate contexts.
- Missing inputs produce an explicit no-setup reason; they are never imputed as zero.

---

### Task 1: Freeze the Research Contract

**Files:**
- Create: `篩選邏輯/early_activation_research.md`
- Create: `篩選邏輯/tests/test_early_activation_score.py`

- [ ] Document definitions, precedence, as-of timing, KPIs, baseline, and non-goals.
- [ ] Write failing tests for all three setups, context mapping, exclusions, precedence, and missing data.
- [ ] Run the focused tests and confirm the failure is the missing module.

### Task 2: Pure Setup Classifier and Evaluator

**Files:**
- Create: `篩選邏輯/early_activation_score.py`
- Modify: `篩選邏輯/tests/test_early_activation_score.py`

- [ ] Implement context mapping and common Early eligibility.
- [ ] Implement mutually exclusive precedence: accumulation retest, reconfirm, then new turn.
- [ ] Implement grouped discovery KPIs: n, +3% hit rate, mean, P50, P90, non-up rate.
- [ ] Implement same-date and same-context matched no-setup baseline.
- [ ] Run focused tests.

### Task 3: Independent Snapshot Ledger

**Files:**
- Create: `篩選邏輯/early_activation_snapshot.py`
- Create: `篩選邏輯/tests/test_early_activation_snapshot.py`

- [ ] Write failing tests for table isolation, `DISCOVERY ONLY`, immutable same-day semantics, and T+1 backfill.
- [ ] Implement an append-only snapshot table with a semantic hash.
- [ ] Implement T+1 close-to-close outcome backfill.
- [ ] Add a research summary reader using only `early_activation_snapshot`.
- [ ] Run focused tests.

### Task 4: Discovery Run and Isolation Verification

**Files:**
- Create: `篩選邏輯/run_early_activation_research.py`
- Create or update: `篩選邏輯/early_activation_research.md`

- [ ] Classify the full T-1 pool without reading T+1 outcomes during classification.
- [ ] Report setup and sector-context KPIs plus matched baseline when outcomes exist.
- [ ] Record data coverage and prevent conclusions when the sample is thin.
- [ ] Run all relevant tests, compile checks, and `git diff --check`.
- [ ] Confirm Opportunity Evidence files have no new diff from this implementation.

