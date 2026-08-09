# Project Agent Rules

## PRIMARY RULE
Unless explicitly told otherwise, continue working autonomously until the requested outcome is complete.
The user should not need to repeatedly say "continue", "fix it", "try again", "then what?", or "finish it".
Treat those follow-up steps as implicit parts of the original task.

Default loop:
DIAGNOSE → FIX → TEST → VERIFY → FIX AGAIN IF NEEDED → COMPLETE → REPORT

Do not stop after reporting an issue. Do not wait for the user after every intermediate step. Return partial progress only when genuinely blocked.

For every task:
1. Understand the requested outcome.
2. Inspect the repository, files, environment, tools, and current project state.
3. Identify the root cause or required implementation.
4. Make the necessary changes.
5. Run appropriate validation: tests, build, lint, browser checks, data checks, production checks, or deployment checks.
6. If validation fails, diagnose and fix it.
7. Re-run validation and repeat until the requested outcome is complete.
8. Review the final git diff and ensure unrelated functionality was not broken.
9. Only then report the final result.

Intermediate failures are not final answers. Investigate, fix if permitted, retry automatically, and continue.

If dependencies or tools are missing, inspect what exists, install/configure if permitted, and retry. Do not ask the user to do work you can do yourself.

If build, test, runtime, data sync, API, browser, or deployment validation fails, read the full error, identify root cause, fix it, rerun, and continue. Never stop after the first failed command.

Before finishing:
- inspect git diff
- confirm only intended files were modified
- preserve existing user work and data
- avoid destructive resets or unrelated deletions

Only stop and ask the user when genuinely blocked by credentials, 2FA, missing external secrets, external authorization, payment, destructive/irreversible actions requiring confirmation, physical access to a powered-off machine, or a platform limitation that cannot be changed from the current environment.

If blocked, report only: exact blocker, what was attempted, the single required user action, and what will continue after that action.

Final report: Completed / Verified / Remaining / User action required.

## Project focus
MLS is the Taiwan stock analysis system. Prioritize correctness of market data, intraday validation, post-market institutional checks, signal logic, history retention, UI state accuracy, and regression safety. Do not silently change trading rules or thresholds; preserve explicit project logic unless the task requests a change.