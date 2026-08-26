# Chip × Price × Market × Intraday Flow — One-Shot Acceptance

Status: **CLOSED — RESEARCH ACCEPTANCE COMPLETE**
Frozen: 2026-08-26
Closed: 2026-08-26
Production impact: **NONE** — production code and Line A unchanged

## 1. Single question

Only answer this:

> Can T-1 institutional history + price/volume/technical structure, then T-day intraday A-flow confirmation, consistently identify the same kind of stocks that later enter WATCH MODE / activate?

This acceptance does not open exact-time studies, new early triggers, or unrelated factors.

## 2. Discovery anchor

Use the 2026-08-26 intraday A19 as a retrospective discovery cohort only.

- A19 = stocks dynamically promoted into the intraday core/A-grade display.
- Other 32 = same fixed 51-stock universe not in that A19 snapshot.
- This comparison is attribution/discovery only. It is NOT evidence that A19 was known at T-1.

## 3. Frozen candidates

### C1 — `STRUCTURE_INTACT`

```text
close >= MA20
```

Interpretation: structural base condition, not assumed to be an independent predictor.

### C2 — `SELLING_PRESSURE_WEAKENING + PRICE_RESPONSE`

Frozen operational definition used in this acceptance:

```text
price_5d > 0
AND close_position >= 0.7
AND NOT(inst_5d <= -3000)
```

Interpretation must remain narrower than the label: this measures that recent institutional selling is not heavy while price has already responded positively and the close sits high in its range. It does not by itself prove a monotonic time-series decline in selling pressure.

### C3 — `PRIOR_BUY_LIGHT_SELL`

A T-1 stock-day qualifies only when ALL are true:

1. `prior_buy_run_days >= 3`
2. `prior_buy_run_sum > 0`
3. `current_sell_run_days` is between 1 and 3 trading days inclusive
4. `current_sell_run_sum < 0`
5. `sellback_ratio = abs(current_sell_run_sum) / prior_buy_run_sum <= 0.30`
6. `close >= MA20`

Interpretation:

> A meaningful institutional buy run is followed by only a short/light sell segment, while the main price structure remains intact. The sell segment may be profit-taking / turnover rather than distribution.

Volume behavior is diagnostic only in this acceptance; it is not part of C3 eligibility.

## 4. Data-integrity rule discovered during execution

A sequencing bug was found and corrected in the T-1 -> T pairing logic.

Incorrect behavior:

> Pair T-1 with the next clean intraday day, which can silently skip intervening trading days and turn `T+1` into `T+N`.

Correct canonical behavior:

> `T+1` means the immediate next **real trading day**. If that next trading day is unusable because intraday/A-flow data are contaminated or missing, the pair is invalid for analyses requiring those intraday fields. Never skip forward to a later clean day and still label the outcome `T+1`.

This rule is part of the acceptance record and must be reused in future sequential research.

## 5. A19 vs 32 discovery matrix

| Candidate | A19 | Other 32 | Enrichment |
|---|---:|---:|---:|
| C1 Structure Intact | 19/19 (100%) | 20/32 (62%) | 1.60x |
| C2 Selling Pressure Weakening + Price Response | 13/19 (68%) | 10/32 (31%) | 2.19x |
| C3 Prior Buy -> Light Sell -> Structure Intact | 2/19 (11%) | 0/32 (0%) | n too small |

The discovery anchor is outcome-conditioned and is not used as standalone proof.

## 6. Correct T+1 activation validation

After fixing real-trading-day adjacency, the maximum available clean T+1 activation sample produced 11 T-1 days for this branch of analysis.

| Candidate | day-equal P(activation \| qualified) | day-equal P(activation \| not qualified) | Days qualified side better |
|---|---:|---:|---:|
| C1 | 34.7% | 21.4% | 7/11 |
| C2 | **60.5%** | **28.9%** | **8/11** |
| C3 | 31.2% | 32.5% | 3/11 |

Additional C1/C2 decomposition retained from the frozen analysis:

```text
C1 only: 29.6% activation
C1 + C2: 64.1% activation
```

Interpretation:

- C1 is primarily a structural eligibility/base condition.
- C2 provides the meaningful incremental activation separation in this sample.
- C3 has no evidence of T+1 activation lift in the available clean intraday sample.

## 7. C3 two-estimand record — do not conflate them

C3 has been tested against two different targets. They must remain separate.

### 7.1 T+1 WATCH MODE / activation

- `n = 22` C3 events across 11 available paired days
- day-equal activation: `31.2%` vs control `32.5%`
- C3 side better on `3/11` days

Final label:

> **NOT SUPPORTED for T+1 activation — INCONCLUSIVE / DATA CEILING**

This is a null-looking result, but the event sample is thin; it is not labeled a decisive rejection.

### 7.2 T+10 Net MFE >= +3%

Independent long-window test, 2020–2025:

- `n = 2,432` C3 events
- hit rate: `60.1%` vs baseline `60.3%`
- average MFE: `+6.7%` vs baseline `+6.7%`
- yearly results oscillate around baseline with no systematic positive deviation

Final label:

> **REJECTED for T+10 Net MFE target**

This is a decisive null for that estimand only. It must not be used to claim a decisive rejection of the separate T+1 activation estimand.

## 8. C2 intraday A-flow confirmation — event/state based

Exact clock time is not used as a signal.

Reuse the existing event/state classes:

- `OPEN_POSITIVE`: A-flow is already positive at the first usable observation; this is left-censored with respect to the true flip event.
- `FLOW_FLIP`: a causal negative/non-positive -> positive transition is observed during the session.
- `NO_FLIP`: no positive transition is observed during the usable session.

For C2-qualified stock-days:

| Flow class | n | Activation rate |
|---|---:|---:|
| OPEN_POSITIVE | 41 | 90.2% |
| FLOW_FLIP | 16 | 62.5% |
| NO_FLIP | 25 | **4.0%** |

Combined confirmation comparison:

```text
confirmed = OPEN_POSITIVE + FLOW_FLIP
confirmed vs NO_FLIP day-equal activation = 89.9% vs 2.8%
direction consistent = 8/9 days
```

Interpretation:

> Intraday A-flow acts as a strong confirmation / contradiction layer for the frozen C2 thesis in this sample. It is not an exact-time entry rule and does not convert `confirmed_reversal` into an automatic buy signal.

Because `OPEN_POSITIVE` is left-censored, this evidence should be described as event/state confirmation rather than proof of when the flow first turned positive.

## 9. Market / sector conditional layer

No acceptance conclusion is made here.

The available Aug validation days used for C1/C2 did not contain enough market-regime variation to identify Risk On / Risk Off conditional effects without inventing distinctions after the fact.

`MARKET_ALIGNMENT` — Market -> Sector -> Stock — is explicitly deferred to the next research cycle. It is not inserted retroactively into this acceptance and it does not modify the frozen C1/C2/C3 results.

## 10. Final labels

| Candidate | Final acceptance label | Meaning |
|---|---|---|
| C1 | **DESCRIPTIVE / BASE CONDITION** | Structural base; marginal contribution weak after separating C2 overlap. |
| C2 | **STRONG CANDIDATE / HISTORICAL SUPPORT** | Strong T+1 activation separation over available clean days; meaningful incremental lift over C1; event/state A-flow provides strong confirmation/contradiction. Not production-supported yet. |
| C3 | **NOT SUPPORTED for T+1 activation — INCONCLUSIVE / DATA CEILING** | n=22 T+1 events show no lift, but sample is thin. |
| C3 on T+10 opportunity target | **REJECTED** | 2020–2025, n=2,432, decisive null for Net MFE >= +3% @ T+10. |

## 11. Production decision

This one-shot acceptance does **not** change production rules.

- Line A remains untouched.
- C1 requires no new production feature; the existing price-structure gate already represents its role.
- C2 is the only candidate worth continued frozen-rule observation.
- C2 may remain `DESCRIPTIVE/WATCH` while more clean forward trading days accumulate.
- C2 must not be promoted directly into an automatic gate or buy signal from this acceptance alone.
- C3 is closed for T+10 opportunity prediction and remains unproven for T+1 activation.
- No C4 or unrelated feature branch is opened by this acceptance.

## 12. Closure

The One-Shot Acceptance is formally closed.

Research cadence after closure:

1. Preserve C1/C2/C3 definitions and this result record.
2. Do not reopen threshold rescue on these same samples.
3. Keep the corrected real-trading-day T+1 pairing rule canonical.
4. Keep intraday confirmation event/state based rather than clock-time based.
5. Open `MARKET_ALIGNMENT` only as a separate next-cycle research question, not as an amendment to this report.
