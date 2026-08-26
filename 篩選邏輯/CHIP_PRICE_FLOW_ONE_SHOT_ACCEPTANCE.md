# Chip × Price × Market × Intraday Flow — One-Shot Acceptance

Status: RESEARCH ONLY
Frozen: 2026-08-26
Production impact: NONE until this acceptance is complete

## 1. Single question

Only answer this:

> Can T-1 institutional history + price/volume/technical structure + market/sector context, then T-day intraday A-flow confirmation, consistently identify the same kind of stocks that later enter WATCH MODE / activate?

Do not branch into time-of-day studies, new early triggers, or unrelated factors.

## 2. Discovery anchor

Use the 2026-08-26 intraday A19 as a retrospective discovery cohort only.

- A19 = stocks dynamically promoted into the intraday core/A-grade display.
- Other 32 = same fixed 51-stock universe not in that A19 snapshot.
- This comparison is attribution/discovery only. It is NOT evidence that A19 was known at T-1.

The purpose is to identify a small number of common T-1 structures worth repeating historically.

## 3. T-1 fields to compare — no extra feature expansion

### A. Institutional history with memory
Do NOT use only current `consecutive_days`, because one sell day erases the prior buy run.

For each stock calculate:

- `prior_buy_run_days`: consecutive institutional buy days immediately before the current sell/neutral segment
- `prior_buy_run_sum`: cumulative institutional net buy during that run
- `current_sell_run_days`: current consecutive sell days
- `current_sell_run_sum`: cumulative institutional sell during that run
- `sellback_ratio`: abs(current sell-run sum) / prior buy-run sum, when prior buy-run sum > 0
- current day institutional net
- institutional 3d / 5d / 20d cumulative net

Interpretation must distinguish:

- long buy run -> 1–3 sell days -> small giveback -> structure intact = profit-taking / turnover candidate
- long buy run -> repeated heavy selling -> large giveback -> price structure breaks = distribution candidate

A sell streak is never negative by itself.

### B. Price / volume / technical structure

Use only existing basic structure:

- price return 1d / 3d / 5d
- volume ratio vs 20d average
- volume expanding vs contracting during sell/pullback days
- close relative to MA5 and MA20
- low trend / whether prior lows hold
- breakout / failed breakout / near-high state
- existing `chip_price_divergence` classification:
  - chip_reversal
  - sell_absorption
  - accumulation
  - washout
  - buying_stall
  - double_weak

No new technical indicator family in this acceptance.

### C. Market / sector context

Include the existing attribution-only context:

- market regime
- market breadth
- pool51 below MA5 %
- pool51 below MA20 %
- sector regime
- sector breadth
- sector relative 3d
- stock relative to sector peers / market peers where available

Purpose: determine whether the same chip/price pattern behaves differently under supportive vs weak market/sector conditions.

Do not turn market context into a production gate during this run.

## 4. T-day intraday comparison

Intraday data is confirmation/context, not a new automatic entry rule.

For each stock after it enters WATCH MODE, compare:

- A-flow sign
- A-flow change vs prior valid 5-minute snapshot
- active buyer ratio when fresh
- current price response to A-flow
- whether price holds/reclaims the T-1 structural reference

The question is:

> Does intraday money flow confirm or contradict the T-1 chip/price thesis?

Do not study 09:15 / 09:30 / exact clock-time effects. Time is only the data-update cadence.

## 5. One-shot workflow

### Step 1 — 2026-08-25 -> 2026-08-26 A19 vs 32
Produce one matrix with the fields above and rank only the strongest common structures.

Expected output:

- prevalence in A19
- prevalence in other 32
- absolute difference
- relative enrichment
- median/mean numeric differences for buy-run length, sellback ratio, volume ratio, MA structure and market/sector context

Do not create rules yet.

### Step 2 — Freeze at most 3 candidate structures
Select at most THREE structures with:

1. clear trading interpretation,
2. obvious A19 vs 32 separation,
3. variables known at T-1 close.

Examples of allowed structure shapes:

- long institutional buy run -> short/light sell run -> shrinking volume -> MA20 intact
- institutional selling -> price refuses to fall -> lows hold -> sector supportive
- low-base accumulation -> price has not responded -> volume controlled -> sector improving

These examples are not pre-approved winners; they only illustrate the form.

### Step 3 — Dedicated validation of `Prior Buy Run -> Short Sell -> Structure Intact`

This pattern gets one direct historical test before any wider feature expansion.

Frozen pattern skeleton:

- prior institutional buy run exists and is meaningfully long
- current sell segment is short (1–3 trading days)
- current selling gives back only a minority of the preceding buy-run accumulation
- price structure remains intact, with MA20 as the main structural reference
- volume behavior during the sell/pullback segment is recorded as expanding vs contracting, but is not used to loosen the event definition after results are seen

Primary validation month:

> **2026-06-01 through 2026-06-30 only.**

July 2026 is explicitly excluded from this test.

Sampling rule:

- Scan the fixed 51-stock universe over all June trading days.
- Collect at least **20 qualifying stock-day events** before judging the pattern.
- Prefer distinct stocks when possible, but do not relax the definition merely to reach 20.
- If June contains fewer than 20 qualifying events, extend **backward into May 2026** until 20 events are reached.
- Do NOT extend forward into July.
- Do NOT tune thresholds after seeing June outcomes.

For each qualifying event report:

- code / date
- prior buy-run days and cumulative buy amount
- current sell-run days and cumulative sell amount
- sellback ratio
- pullback-day volume ratio / volume contraction or expansion
- close vs MA5 / MA20
- whether lows remain structurally intact
- market regime / breadth
- sector regime / relative strength
- T+1 WATCH MODE / activation outcome
- T+1 max favorable move
- T+1 close return
- T+1 intraday A-flow confirmation or contradiction when clean data exists

Primary question:

> Does this pattern produce a materially higher T+1 activation rate than the same-month fixed-51 baseline?

Secondary question:

> When it activates, does T-day A-flow tend to confirm the prior institutional/price thesis rather than contradict it?

### Step 4 — Historical repeat of any remaining frozen candidates
Only after the dedicated June test above is complete, run any other frozen candidate structures on clean historical days.

Primary target:

- probability of entering T-day WATCH MODE / confirmed activation

Secondary descriptive outcomes:

- T-day max favorable move
- T-day close return
- whether intraday A-flow confirmed vs contradicted the T-1 structure

Use stock-day and day-equal summaries. Do not treat 5-minute snapshots as independent samples.

No threshold tuning after seeing these historical results.

## 6. Acceptance

A candidate can be marked `SUPPORTED` only if all are true:

1. It is materially enriched in the A19 discovery cohort versus the other 32, OR for the dedicated prior-buy-run pattern, it shows clear lift in the frozen June historical test.
2. Historical clean-day activation lift remains in the same direction on a clear majority of days.
3. The effect is not explained only by one sector or one market-regime day.
4. The interpretation remains economically coherent after including market/sector context.
5. T-day intraday A-flow behaves as a useful confirmation/contradiction layer rather than requiring future information.

If the A19 separation disappears historically, mark `SINGLE-DAY ANOMALY / REJECTED` and stop.

If direction persists but sample/effect is weak, mark `DESCRIPTIVE / WATCH`, do not add gates.

## 7. Explicit stop rules

During this acceptance, DO NOT:

- research exact clock times
- invent earlier activation predictors
- revive Persistent Flow Flip as the main line
- add new indicator families because one table looks interesting
- use current-day dynamic A19 membership as if it were T-1 selection
- automatically penalize one or two institutional sell days
- use July 2026 for the dedicated prior-buy-run validation
- change Line A
- modify production selection/entry logic

Interesting results may be recorded, but unrelated branches are deferred until this acceptance closes.

## 8. Final deliverable

One final report only:

1. A19 vs 32 common-signal matrix
2. dedicated June 20-event test for `Prior Buy Run -> Short Sell -> Structure Intact`
3. the frozen top 1–3 structures
4. historical repeat table by day
5. market/sector conditional view
6. intraday A-flow confirmation view
7. final label for each candidate: `SUPPORTED / DESCRIPTIVE / REJECTED`
8. one sentence on whether any finding deserves later production testing

No additional research branch should be opened before this report is complete.
