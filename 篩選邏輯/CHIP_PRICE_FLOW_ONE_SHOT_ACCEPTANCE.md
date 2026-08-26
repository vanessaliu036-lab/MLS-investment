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

Current candidates:

- C1 `Structure Intact`
- C2 `Selling Pressure Weakening + Price Response`
- C3 `Prior Buy Run -> Short Sell -> Structure Intact`

Do not open C4 before this acceptance closes.

### Step 3 — C3 dedicated validation: June -> January, month by month

C3 is now frozen and must be traced **backward from June 2026 through January 2026**. July is excluded. Do not stop after seeing June.

#### Frozen C3 event definition

A T-1 stock-day qualifies only when ALL are true:

1. `prior_buy_run_days >= 3`
2. `prior_buy_run_sum > 0`
3. `current_sell_run_days` is between **1 and 3** trading days inclusive
4. `current_sell_run_sum < 0`
5. `sellback_ratio = abs(current_sell_run_sum) / prior_buy_run_sum <= 0.30`
6. T-1 close is **at or above MA20** (`close >= MA20`)

This is the frozen eligibility definition. Do not alter these thresholds after seeing outcomes.

Interpretation:

> A meaningful institutional buy run is followed by only a short/light sell segment, while the main price structure remains intact. The sell segment is treated as a possible profit-taking / turnover event, not automatically as distribution.

#### Volume is diagnostic, not eligibility

For every C3 event, separately record whether the sell/pullback segment is:

- volume contracting
- volume normal
- volume expanding

Do **not** use volume to admit/exclude C3 events in this run. First test whether volume behavior explains which C3 cases work.

#### Required calendar walkback

Run the fixed 51-stock universe separately for:

- 2026-06-01 through 2026-06-30
- 2026-05-01 through 2026-05-31
- 2026-04-01 through 2026-04-30
- 2026-03-01 through 2026-03-31
- 2026-02-01 through 2026-02-28
- 2026-01-01 through 2026-01-31

**July 2026 must not be used.**

Do not pool the months first. Each month must be reported independently so regime dependence and month-to-month stability remain visible.

If one month has fewer than 20 C3 events, report the true count. Do not loosen C3 to manufacture 20 events. The six-month aggregate can then provide the larger event count.

#### Per-event fields

For every qualifying event report/store:

- code / T-1 date
- prior buy-run days
- prior buy-run cumulative institutional buy
- current sell-run days
- current sell-run cumulative institutional sell
- sellback ratio
- T-1 1d / 3d / 5d price return
- sell/pullback volume behavior and volume ratio
- close vs MA5
- close vs MA20
- whether prior lows remain intact
- market regime / breadth
- sector regime / breadth / relative strength
- T-day WATCH MODE / confirmed activation outcome
- T-day max favorable move
- T-day max adverse move when available
- T-day close return
- T-day intraday A-flow confirmation / contradiction when clean data exists

#### Required monthly table

For EACH month Jan–Jun report at minimum:

- C3 event count
- distinct stock count
- number of trading days containing C3 events
- `P(T-day activation | C3)`
- same-day eligible control activation rate
- activation lift in percentage points
- day-equal activation lift
- number / share of days where C3 beats control
- mean/median T-day MFE
- mean/median T-day MAE when available
- mean/median T-day close return
- volume-contracting vs volume-expanding descriptive split
- market / sector regime distribution

Also report one six-month summary using both:

1. pooled stock-day statistics, and
2. **month-equal** averages so one high-event month cannot dominate the conclusion.

#### C3 acceptance logic

C3 is not judged from one month alone.

Mark `SUPPORTED` only if:

- activation lift is positive in a clear majority of Jan–Jun months,
- the month-equal activation lift is positive and economically meaningful,
- results are not carried by one stock / one sector / one month,
- the pattern remains coherent after viewing volume and market/sector context.

Mark `DESCRIPTIVE / WATCH` if direction is mostly positive but event count/effect size is weak.

Mark `REJECTED` if the effect is inconsistent or negative across the Jan–Jun walkback.

No threshold rescue after the six-month result is seen.

### Step 4 — Historical repeat of C1 / C2

C1 and C2 remain separately frozen. Their previously observed Aug clean-day results may be reported, but do not change their definitions while C3 Jan–Jun validation is running.

Primary target:

- probability of entering T-day WATCH MODE / confirmed activation

Secondary descriptive outcomes:

- T-day max favorable move
- T-day close return
- whether intraday A-flow confirmed vs contradicted the T-1 structure

Use stock-day and day-equal summaries. Do not treat 5-minute snapshots as independent samples.

No threshold tuning after seeing historical results.

## 6. Acceptance

A candidate can be marked `SUPPORTED` only if all are true:

1. It shows material activation lift under its frozen historical validation.
2. Historical clean-day / clean-month lift remains in the same direction across a clear majority of independent periods.
3. The effect is not explained only by one stock, sector, month, or market-regime episode.
4. The interpretation remains economically coherent after including market/sector context.
5. T-day intraday A-flow behaves as a useful confirmation/contradiction layer rather than requiring future information.

If direction persists but sample/effect is weak, mark `DESCRIPTIVE / WATCH`, do not add gates.

## 7. Explicit stop rules

During this acceptance, DO NOT:

- research exact clock times
- invent earlier activation predictors
- revive Persistent Flow Flip as the main line
- add new indicator families because one table looks interesting
- use current-day dynamic A19 membership as if it were T-1 selection
- automatically penalize one or two institutional sell days
- use July 2026 for C3 validation
- stop C3 after June; continue month by month through January
- loosen C3 thresholds to increase sample size
- change Line A
- modify production selection/entry logic

Interesting results may be recorded, but unrelated branches are deferred until this acceptance closes.

## 8. Final deliverable

One final report only:

1. A19 vs 32 common-signal matrix
2. C1 / C2 frozen historical results
3. C3 month-by-month validation table for **June, May, April, March, February, January 2026**
4. C3 six-month pooled + month-equal summary
5. volume contraction / expansion interpretation inside C3
6. market/sector conditional view
7. intraday A-flow confirmation view
8. final label for C1 / C2 / C3: `SUPPORTED / DESCRIPTIVE / REJECTED`
9. one sentence on whether any finding deserves later production testing

No additional research branch should be opened before this report is complete.
