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

## 11. Risk-adjusted participation amendment

Amendment recorded: **2026-09-02**

This amendment changes the post-activation objective from **avoid-error first** to
**maximize risk-adjusted return**. It does not change the frozen C1/C2 definitions,
the T-1 selection cohort, or the meaning of `confirmed_reversal`.

The system must not treat a single overheating signal as an automatic rejection.
The question is whether the expected opportunity is large enough to justify the
risk, and if so, what position size and confirmation standard are appropriate.

### 11.1 Separate lifecycle, opportunity, risk, and action

These dimensions must remain separate:

```text
Lifecycle:  WATCH → ARMED → ACTIVE → MOMENTUM
Risk:       NORMAL / EXTENDED / EXHAUSTED
Action:     WAIT / ENTER / MOMENTUM_ENTRY / INVALIDATED
```

- `WATCH`: structure is being observed; activation is not complete.
- `ARMED`: trigger is approaching or has begun to form, but the full activation
  evidence is not yet complete.
- `ACTIVE`: `PRICE TRIGGER + VOLUME QUALITY + ACCEPTANCE` are all confirmed.
- `MOMENTUM`: an `ACTIVE` stock also shows continuation evidence: strong sector,
  positive A-flow, `RVOL > 1.5x`, sustained acceptance, and/or new-high
  acceleration. `MOMENTUM` is a continuation state, not permission to chase
  without conditions.

`EXTENDED` is a risk overlay, not a lifecycle failure. It may reduce position size
and require stronger confirmation, but it must not by itself remove a valid
opportunity. `EXHAUSTED` is the only risk overlay that can prohibit a new entry,
and it requires evidence of actual deterioration rather than price strength alone.

### 11.2 Opportunity and risk scores

The decision layer should expose two independent 0–100 scores:

```text
Opportunity Score = flow + sector strength + relative strength
                    + volume quality + breakout quality + acceleration

Risk Score        = MA5/MA20 deviation + consecutive up-days
                    + volume-exhaustion risk + pullback risk + chip divergence

Edge = Opportunity Score − Risk Score
```

`Risk Score` is not an automatic rejection score. A high risk score means the
system must demand better entry quality and/or reduce size. A stock is rejected
only when the evidence indicates that the opportunity no longer compensates for
the risk, or when the structure has failed.

### 11.3 Extension and exhaustion rules

The old rule “third consecutive up day means do not trade” is replaced with:

> **Third consecutive up day means no unconditional chase.**

`EXTENDED` means the stock may still be tradable, but only with a tactical
position and a higher confirmation threshold. The default initial position for a
valid momentum entry is **one-third of the normal position**; additional size
requires continued A-flow, volume, and acceptance evidence.

`EXHAUSTED` requires a combination such as:

```text
overextended price
AND A-flow turns negative or materially weakens
AND volume expands without price progress
AND price loses VWAP and/or the trigger level
```

No single condition—three up days, high MA5 deviation, high RVOL, or a new
high—is sufficient to label a stock `EXHAUSTED`.

### 11.4 Momentum entry modes

For `MOMENTUM` candidates, the system may recognize the following entry modes:

1. **First effective pullback**: approximately 1–2% pullback, A-flow remains
   materially positive, and price re-strengthens after the pullback.
2. **VWAP hold and reclaim**: price holds near VWAP, then reclaims strength with
   renewed volume.
3. **Accelerating new high**: price makes a new high together with fresh A-flow
   acceleration and volume expansion. This permits only a one-third initial
   position; it is never a full-size unconditional chase.

The entry does not require a complete retest of the original breakout price.
The preferred confirmation is the first valid pullback/reclaim or continuation
retest that preserves VWAP, trigger, A-flow, and acceptance quality.

### 11.5 Canonical example: 2455 全新

The following is a policy example, not a standalone validation result. A stock
with approximately `+21%` over three days and `+18.5%` above MA5 may still qualify
for `MOMENTUM CONTINUATION` when it also has strong RVOL, positive and accelerating
A-flow, supportive 5-day/institutional flow, strong sector strength, valid
breakout, and sufficient acceptance.

The correct output is:

```text
2455 全新：MOMENTUM CONTINUATION
Action：可交易，但禁止直接追紅棒
Entry：優先等待第一次有效回撤；正常部位 1/3 起手，資金續強再加
```

This replaces the blanket wording “禁止追價” for this specific high-edge,
high-extension situation. It does not override a later `EXHAUSTED` or
`INVALIDATED` state.

### 11.6 Validation requirement

This amendment is the canonical design target for the next forward-validation
cycle. Until independent forward data validate the thresholds and position-size
rules, the UI and reports must label these as **research/design policy**, not as
guaranteed trading signals. The validation must separately measure continuation
payoff, MAE, slippage, and results by `NORMAL`, `EXTENDED`, and `EXHAUSTED`.

## 12. Product placement: 機會雷達

The risk-adjusted continuation policy belongs directly in the existing MLS
**機會雷達** page. This page already contains the individual-stock screening
and trade-decision table, including `CHIP`, `FLOW`, `PRICE TRIGGER`, `VOLUME`,
`ACCEPTANCE`, `EXTENSION RISK`, `TRADE STATE`, and `ACTION`. It must not become
another stock-selection gate or a separate standalone page. The new fields are
placed after `EXTENSION RISK` and before `TRADE STATE`.

Canonical column order:

```text
EXTENSION RISK → MOMENTUM STATE → OPPORTUNITY SCORE → POSITION RULE → TRADE STATE → ACTION
```

### 12.1 Responsibility boundaries

- The upstream radar layer finds the opportunity context: capital flow, sector,
  leader, stock, and AI signals.
- **FINMIND／Chips** remains the data layer for institutional and positioning
  facts; it does not decide whether an extended momentum entry is executable.
- **Sector** supplies sector confirmation; it does not decide the entry state.
- **Trigger** answers only whether the breakout occurred.
- **機會雷達** answers whether the already-strong stock remains worth trading
  and how much capital to deploy.
- **盤後驗證** only checks whether the day's Momentum decision succeeded or
  failed and feeds those outcomes back into model calibration; it does not make
  the live entry decision.

`EXTENSION RISK` remains visible, but is explicitly an execution overlay rather
than a veto. The 機會雷達 table must show lifecycle, extension, momentum,
opportunity, risk, and position sizing as separate facts without adding a new
page.

### 12.2 Required 機會雷達 fields

| Field | Required values / meaning |
|---|---|
| `MOMENTUM STATE` | `NONE / CONTINUATION / ACCELERATION / EXHAUSTED` |
| `OPPORTUNITY SCORE` | `LOW / MEDIUM / HIGH / VERY HIGH`, backed by the underlying opportunity factors |
| `POSITION RULE` | `FULL / 1/2 / 1/3 START / NO TRADE` |
| `EXTENSION RISK` | `LOW / MEDIUM / HIGH`; it remains an overlay, not a veto |
| `EXTENSION STATE` | `NORMAL / EXTENDED / EXHAUSTED`; the state is separate from risk severity |
| `TRADE STATE` | Existing lifecycle/action state, including `MOMENTUM` when continuation is confirmed |
| `Action` | e.g. `WAIT`, `BUY ON FIRST VALID PULLBACK`, `FLOW RE-ACCELERATION`, `NO CHASE` |

The table should also retain the numeric `Opportunity Score`, `Risk Score`, and
`Edge` when those inputs are available. Labels such as `HIGH` and `VERY HIGH` are
display abbreviations and must not replace the underlying values.

### 12.3 Canonical table interpretation

```text
ACTIVE + EXTENDED + STRONG FLOW + STRONG SECTOR
    → MOMENTUM CONTINUATION
    → Position Rule: 1/3 START
    → Action: BUY ON FIRST VALID PULLBACK / FLOW RE-ACCELERATION

EXTENDED + FLOW DECELERATING + VOLUME CLIMAX + VWAP LOSS
    → EXHAUSTED
    → Position Rule: NO TRADE
    → Action: NO CHASE
```

For the 2455 全新 policy example, 機會雷達 displays:

```text
Opportunity：HIGH
Extension：EXTENDED
Momentum：CONTINUATION
Position：1/3 START
Action：BUY ON FIRST VALID PULLBACK / FLOW RE-ACCELERATION
```

This is an execution interpretation of an existing opportunity, not a change to
the upstream selection facts. No new page is required. 盤後驗證 records the
forward result separately for model calibration.
