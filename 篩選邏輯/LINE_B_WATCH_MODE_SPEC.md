# Line B — WATCH MODE / Remaining Payoff Research Spec

Status: RESEARCH ONLY
Date frozen: 2026-08-26
Production impact: NONE

## 1. Objective

Line B no longer tries to predict an activation 30–60 minutes before it happens.
The practical objective is:

> Once a stock has genuinely activated, use the available 5-minute intraday updates to judge whether the remaining payoff is still larger than chase risk.

This is a post-activation decision problem, not an early-prediction problem.

## 2. Three-layer timeline

### Layer 1 — T-1 Frozen Selection
Use only the prior close persisted snapshot/tier/payload. This layer must be immutable and must never contain T-day intraday information.

### Layer 2 — Activation / WATCH MODE
`confirmed_reversal` is an activation-state transition only.

Current canonical mechanism in `decision_view.py`:

```python
confirmed_reversal = (
    classification in (TIER_REVERSAL, TIER_NO_CHASE, TIER_CANDIDATE)
    and None not in (close, prior_high, ma20, flow)
    and close > prior_high
    and close >= ma20
    and flow > 0
)
```

When it becomes true:

> ENTER WATCH MODE

It MUST NOT be interpreted as BUY / ENTER.

The intraday `display_pool='core'` is a dynamic current-state label. It is not an overnight frozen selection cohort and must not be used to measure T-1 selection hit rate.

### Layer 3 — Entry Decision after activation
After WATCH MODE begins, subsequent 5-minute observations are used to classify the stock into exactly one of four operational states:

- `ENTER` — activation remains structurally healthy and expected remaining payoff is worth the chase risk.
- `WAIT` — stock remains strong but current price/location is unattractive; wait for a valid pullback/reclaim or stronger continuation evidence.
- `NO_CHASE` — extension/chase risk is too high even if price may continue rising.
- `INVALIDATED` — price and/or flow structure has materially failed after activation.

These states are research outputs until validated. They are not production trading rules yet.

## 3. Inputs allowed after WATCH MODE

Use only information observable at or before each 5-minute decision timestamp:

- current price and current day return
- distance from current day high
- current A-flow / `net_active`
- change in A-flow from the previous valid 5-minute snapshot
- active-buyer ratio when available and fresh
- incremental volume / volume change, not hindsight full-day volume
- whether a new high was made, or price is in a pullback
- T-1 frozen `MA20`, prior high, trigger/reference levels
- first causal pullback and whether price subsequently reclaims

No future high/low, end-of-day information, or later snapshot may be used to classify an earlier timestamp.

## 4. Primary research question

Do NOT ask:

> Will this stock rise today?

Ask:

> Given that activation is already confirmed, does this exact timestamp still have enough remaining upside relative to downside to justify entry?

Primary comparison is `remaining payoff` versus `chase risk`.

## 5. Outcomes

From each causal decision timestamp measure forward-only outcomes:

- Net MFE +15m / +30m / +60m / to-close
- Net MAE +15m / +30m / +60m / to-close
- terminal net return for the same horizons
- P(Net MFE >= +2%)
- P(Net MFE >= +3%)
- pre-decision return already realized before the timestamp
- upside/downside asymmetry (`Net MFE` versus `|Net MAE|`)

Trading cost assumptions must remain consistent with the existing Line B research convention.

## 6. Research sequencing

1. Treat `confirmed_reversal` as WATCH MODE start, not entry.
2. For every later valid 5-minute snapshot, record the causal state variables above.
3. Separate paths that later continued from paths that later rolled over, but never use the later outcome to define the earlier rule.
4. Search for the smallest stable state transition that separates `ENTER / WAIT / NO_CHASE / INVALIDATED`.
5. Once a candidate rule is selected, freeze the definition before evaluating an independent period.
6. Do not threshold-rescue a failed candidate.

## 7. Persistent Flow Flip status

`Persistent Flow Flip` remains a research benchmark only:

- It is earlier than `confirmed_reversal` in the current sample.
- Its forward metrics are directionally better than `confirmed_reversal`.
- Its absolute terminal payoff is still insufficient and its eligible coverage is limited.

Current status:

> `BORDERLINE — directionally better, not production ready`

Do not add volume or extra thresholds to rescue it. It may remain in head-to-head tables as an alternate activation timestamp, but it is not the main research objective.

## 8. Data integrity constraints

- Keep the existing 5-minute cadence; do not design around unavailable 1-minute/tick data.
- Do not weaken the opening blind period merely to manufacture earlier triggers.
- Use only dates/snapshots that pass the A-flow freshness/data-integrity audit.
- Exclude known contaminated dates from sequential A-flow research until source freshness is trustworthy.
- Never mix T-1 frozen selection labels with T-day dynamic `display_pool` labels in performance calculations.

## 9. Explicitly out of scope

The following are not current research goals:

- predicting activation 30–60 minutes in advance
- competing with manual tick-by-tick discretionary traders
- turning `confirmed_reversal` directly into a buy signal
- reopening rejected near-MA5 / quiet-volume / persistent-outflow selection hypotheses
- changing frozen Line A opportunity evidence
- production/UI rule changes before research validation

## 10. Decision principle

The desired workflow is:

```text
T-1 Frozen Selection
        ↓
confirmed_reversal / genuine activation
        ↓
WATCH MODE
        ↓
5-minute state updates
        ↓
ENTER / WAIT / NO_CHASE / INVALIDATED
```

Success is not defined as finding the earliest possible activation.

Success is:

> At the first practically observable point after genuine activation, correctly identify whether enough tradable upside remains to compensate for the downside and chase risk.
