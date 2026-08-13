# Decision View Model Design

## Goal

Make classification, entry timing, ranking, and explanatory copy agree across
the POST watchlist, intraday watchlist, funnel, review history, and mobile UI.
The existing four-gate structural-failure rule remains unchanged.

## Single backend view model

Every stock row exposes one canonical decision view:

- `classification`: one of the existing five model states.
- `display_pool`: `core`, `reversal`, `pullback`, `watch`, or `rejected`.
- `potential_grade`: stock potential independent of timing.
- `trend_stage`: lifecycle stage independent of entry quality.
- `entry_quality`: `A`, `B`, `C`, or `D`.
- `entry_state`: `not_triggered`, `near_trigger`, `triggered`,
  `trigger_failed`, `no_chase`, or `wait_pullback`.
- `entry_state_label`: plain-language status shown by the UI.
- `next_upgrade_condition`: the next observable condition that upgrades the
  stock; it must not contain margin-financing language.
- `reason_tags`: at most three short reasons that explain potential or price
  setup, never margin financing.
- `chip_tags`: financing and institutional facts shown separately.
- `ranking_factors`: pressure absorption, buying efficiency, and chip reversal
  acceleration.

The UI must consume these fields without reclassifying stocks.

## Pool mapping

- `🔥 A級啟動` -> main/core pool.
- `🔄 反轉候選` -> reversal pool.
- `⏳ 強勢但不追` -> wait-for-pullback pool.
- `👀 保留觀察` -> watch pool.
- `❌ 結構失效` -> rejected pool and optional Recovery Scan.

An A-grade stock may never appear under copy that says it failed the selection
threshold. A reversal candidate upgrades to A when price is above MA20, active
flow is positive, and price has broken the prior high. A limit-up move with
those confirmations is therefore A, while entry timing may still be no-chase.

## Reasons and chip labels

Margin financing is never an entry reason, upgrade condition, or primary
ranking explanation. It remains a scoring feature for compatibility and is
published only as a chip tag:

- Positive change: `融資：+N ⚠ 槓桿增加`.
- Negative change: `融資：-N 籌碼降槓桿`.
- Missing: `融資 —`.

Entry reasons come from price and demand behavior: prior-high breakout, MA20
reclaim, price-volume confirmation, active-flow turn, sector strength, or a
successful support retest. Long sentences are reduced to at most three tags.

## Entry state machine

The state is independent from stock potential:

- `尚未觸發`: valid trigger exists and price remains more than 1% away.
- `接近觸發`: price is within 1% below the trigger; show the exact distance.
- `已觸發`: current price is at or above trigger.
- `觸發後失敗`: the intraday high touched the trigger but current price fell
  back below it.
- `禁止追價`: the classifier marks no-chase or the stock is at its exact daily
  limit.
- `等待回測`: wait-for-pullback classification or an engine/retest track.

The UI shows trigger, current price, and distance rather than “待進場日盤中”.

## Ranking factors

Institutional streak is a feature, not the primary order. Tomorrow-watch order
uses three normalized 0-100 diagnostics:

- pressure absorption: negative active flow or institutional selling with
  limited price damage and a firm close position;
- buying efficiency: positive active flow translated into price appreciation,
  penalizing high flow with weak price response;
- chip reversal acceleration: current active flow minus previous flow,
  strengthened by a price breakout or MA20 reclaim.

`decision_rank_score` is the weighted mean of available diagnostics. Missing
diagnostics do not become zero and are listed as pending. Classification pool
is the primary sort key; the new score orders stocks inside each pool.

## Mobile UI

The main watch section begins with four expandable summaries: A grade,
reversal, wait for pullback, and watch. Only the selected group expands. The
uniform source column is removed. Each row shows potential, trend, entry
quality, entry state, up to three reason tags, next upgrade condition, and a
compact metrics strip: institution, active flow, margin, and volume ratio.
Missing metrics render as a grey dash and details remain available on stock
open.

## Verification

Tests must prove that margin never appears in reasons, classifications map to
exactly one pool, confirmed reversals upgrade to A, entry states cover all six
states, ranking factors do not use institutional streak as the primary key,
and mobile HTML uses the canonical fields without a source column.
