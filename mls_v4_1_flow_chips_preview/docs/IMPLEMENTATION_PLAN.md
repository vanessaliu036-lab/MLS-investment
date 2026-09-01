# MLS v4.1 Flow × Chips Preview Implementation Plan

Goal: Build an isolated, DB-only preview plugin for intraday inflow/outflow TOP10 with v4.1 freshness, acceptance, persistent-flow, volume-quality, rescue and historical-probability analysis.

Architecture: The preview never imports or mutates MLS engine/scoring/chips modules. It reads an external SQLite source database in read-only mode when needed and stores only plugin-owned snapshots/history in a separate plugin SQLite database. A standalone FastAPI app serves an independent HTML page and JSON API; no existing MLS route or schema is modified.

Global constraints:
- Existing MLS files: zero modifications.
- VPS: no deployment or connection in this phase.
- Stale price data blocks FAIL/RESTART/ACTION and forces OBSERVE_ONLY.
- Rescue remains observation-only until out-of-sample validation gate is approved.
- Historical percentages are hidden when n < 20.
- No fallback that silently substitutes stale or guessed data.

Tasks:
1. Add deterministic v4.1 rule primitives with tests: freshness, CLV validity, acceptance fallback, volume-quality matrix.
2. Add flow-window and chip-persistence calculations with tests.
3. Add four-quadrant + rescue decision analyzer with tests, including STALE and regime suspension.
4. Add historical scenario statistics with minimum-sample guard and rescue validation gate.
5. Add plugin-owned SQLite schema/repository and DB-only TOP10 query path.
6. Add standalone FastAPI preview API + mobile HTML page.
7. Add demo seed for local preview only and README integration notes.
8. Run pytest and syntax checks before publishing files to the isolated Git branch.
