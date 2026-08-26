# Early Activation Research

**Evidence status: `DISCOVERY ONLY`**

Early Activation answers a different question from Opportunity Evidence:

- Opportunity Evidence: is there a tradable +3% opportunity over roughly T+10/T+15?
- Early Activation: at T0 close, does the stock look close to activating over the next few days?

The two pipelines must remain independent. This research must not modify or write `opportunity_snapshot`, alter `sec_rs_10d @ Top10%`, change Opportunity tiers, or blend both lines into one score.

## As-of and outcome

- Classification uses only facts available at T0 close and the preceding five trading days.
- The first discovery outcome is `T+1 close / T0 close - 1`.
- `hit_plus_3` means the T+1 close-to-close return is at least +3%.
- This is an identification KPI, not an executable return; no transaction cost is deducted.
- T+1 outcome columns must never be passed into the classifier.

## Common Early eligibility

All three setups require:

- `abs(MA5 distance) <= 2%` at T0;
- volume ratio `< 1.2x` at T0;
- a complete foreign-streak, MA5, volume, and sector input;
- sector regime is not `RISK_OFF`.

The constants are imported from `pre_activation.py`; this research does not tune new thresholds to the four observed stocks.

## Mutually exclusive candidate definitions

Precedence is `ACCUMULATION_RETEST` → `RECONFIRM` → `NEW_TURN`.

### A. `NEW_TURN`

- T0 foreign streak is two or three consecutive buy days;
- the trading day immediately before that current run had a foreign streak `< 2`;
- common Early eligibility passes.

The two-to-three-day window treats the turn as fresh without requiring the user to see it on exactly the first qualifying day.

### B. `RECONFIRM`

- T0 foreign streak is at least two days;
- in T-5 through the day before the current run, there was at least one positive foreign day/sequence;
- after that earlier positive observation, at least one non-positive interruption occurred;
- the current positive run then resumed;
- common Early eligibility passes.

This definition is frozen before broader backtesting. A prior one-day positive observation qualifies; future research may compare stricter variants, but it must not overwrite this rule version.

### C. `ACCUMULATION_RETEST`

- T0 foreign streak is at least five days;
- all available days in the current five-day accumulation window remain positive;
- within T-5 through T-1, price was at least 7% above MA5 on one day;
- T0 price has reconverged to within ±2% of MA5;
- common Early eligibility passes.

The 7% threshold reuses the existing `MA5_HOT` boundary and is not fitted to 3037.

## Sector context

- `RISK_ON`: the existing strict production sector regime is `RISK_ON`.
- `TURNING_POSITIVE`: not strict `RISK_ON`, but sector return is `> 0` and advancing breadth is `>= 50%`.
- `NEUTRAL`: neither condition is met and the sector is not `RISK_OFF`.
- Existing `RISK_OFF`: no Early Setup; stored as an explicit exclusion reason.

## Discovery KPIs

For each Setup × Sector Context and for overall setups:

- sample size `n`;
- T+1 +3% hit rate;
- average T+1 return;
- P50 and P90 T+1 return;
- non-up rate (`T+1 return <= 0`).

The matched baseline is made only from no-setup stocks on the same date and in the same Sector Context. It reports identical KPIs and never uses future outcomes to select matches.

## Interpretation guardrail

The initial four examples were selected after they rose. They are motivating cases, not validation. Until the rules are run prospectively or on a predeclared historical sample with adequate counts, UI and reports must show `DISCOVERY ONLY` and must not present a probability, confidence, recommendation, or combined Opportunity/Activation score.

