# Fast Turn Watch v2 — research-only execution spec (2026-10-14)

## Scope and preserved constraints
- Independent 1–2 **trading-session** stock research extension. Price must be <= TWD 1,000; fixed holdings/watchlist can be reviewed separately from new candidate discovery.
- Do NOT modify the frozen Line A `sec_rs_10d @ Top10%`, `opportunity_snapshot`, Line B `early_activation_score.py`, or their ledgers, baselines and claims.
- Existing pre-activation and Recovery Scan are NOT replaced. This additional model targets the first tradable rebound after a shock, lower-shadow absorption, early price/flow divergence, leader retest, or sector change.
- Outputs are `DISCOVERY ONLY` with `trade_approved=False`. Never place orders or present experimental matches as verified strategy edge.

## Candidate timing
- T-1 closing data is immutable, classified before T+1 opens; no current-session high, return, closing price or outcome can enter this snapshot. Preserve rejected/no-setup universe rows for matched baseline testing.
- Additional T+1/T+2 intraday confirmation needs event timestamps from the **original data provider**, not refresh time in UI. Compare source timestamps on price, A-flow, and sector breadth separately.
- A-flow (trade-aggression proxy) is **not** official net institutional flow. Official institutional net buys remain T-1 EOD and must carry `institutional_date`.
- Sector breadth is the proportion of constituent stocks advancing, NOT the count of A-flow-positive constituents. Cash flow amounts (in TWD, if from an externally published sector ranking) are not interchangeable with A-flow shares/volume.
- Missing, stale, unmatched source date, future-dated events and stale repeated values -> `NO_DATA`, never numeric zero and never extrapolated trend.

## EOD research candidates
1. `SHOCK_ABSORPTION`: daily loss >=5% plus recovery at least 45% of daily high-low range.
2. `LONG_LOWER_SHADOW`: low <= previous close -3%, high daily close location >=70%, daily return between -4% and +2%.
3. `PRICE_FLOW_DIVERGENCE`: same-day verified institutional net positive while price -2% to +1.5%, volume >=0.8x five-day average.
4. `LEADER_RETEST`: previous five-day rise >=7%, daily pullback -5% to 0%, close in upper 40% of daily range.
5. `SECTOR_NEW_TURN`: same-date sector positive, breadth transitions below 50% to >=55%, stock not already >+2%.

These thresholds are PRE-REGISTERED RESEARCH HYPOTHESES, not optimized to retrospectively explain the 2026-10-14 winners. Rejected rows remain visible; missing optional evidence never counted as positive.

## Intraday confirmation
- At least **two independent live observations** and valid T+1 or T+2 trading-session age (computed by upstream exchange calendar).
- Source timestamps: quote/A-flow <=120s old, sector <=180s old. Quote must belong to the current Taiwan trading date.
- Price reclaims live open+VWAP from session low (>=1.2%) OR crosses the frozen T-1 high; A-flow >0, current relative volume >=1.1x, sector return >0 with >=55% advancing constituents.
- Intraday minimum-session-low broken below frozen T-1 low, quote below T-1 low, or market RISK_OFF -> invalidated or risk blocked.
- >=+6% daily move, reaching first resistance, or prospective reward/risk <1.5 -> `NO_CHASE`.
- `READY_FOR_MANUAL_REVIEW` is a research review label, not an executable order. Explicit price, invalidation, resistance references are required and derived from observed facts.

## Operations and audit
- Before any production wiring: test actual Shioaji subscription payload mapping, current quote `aflow_ts` and sector-provider timestamps, source `resistance`, `sessions_since_candidate`, and 2 live independent observations with a deterministic event ID (no duplicate snapshots).
- Append-only table (separate from current ledgers): `fast_turn_candidate_v2` with `data_date,code,signal_type,version,asof,raw_fact_hash,reasons`; record `T+1/T+2` event statuses and outcomes separately. Never overwrite T-1 snapshot after seeing winners.
- Evaluate >= 60 trading sessions / multiple regimes before any official recommendation: precision, false-positive rate, missed-winner rate, signal lead time, T+1 and T+2 average/p50/left-tail returns, intraday adverse excursion, after-fee/slippage return, event coverage and outage counts. Compare matched same-sector/date no-signal candidates. Reserve chronological out-of-sample period; no winner-only backfill or hindsight reconstruction.
- For the 2026-10-14 reversal, mark screenshot-only industry flows `SOURCE_TIME_UNVERIFIED` unless raw provider timestamp independently validates a new event. The 9:12 and 9:44 identical flow values are NOT evidence of new outflow/inflow. Price reversal is separate.
- The frozen Line A and existing Line B have their own evidence metrics. Never blend these reports or reuse their score to claim research validation.

## Verification and release gates
- Local command: `python -m unittest discover -s tests -p test_fast_turn_watch.py -v`.
- New test fixtures are **synthetic**, not claims of historic pre-breakout price data.
- Code in this PR is **not integrated into production API, DB or notifications**. Production rollout requires data-mapping tests, real as-of backtest, paper-trade alert QA and explicit release check. No VPS service restart, SSH edits or secrets.
