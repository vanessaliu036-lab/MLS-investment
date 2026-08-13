#!/usr/bin/env bash
# Explicitly adopt the current VPS AB source into the canonical git directory.
set -euo pipefail

SCREEN_LOCAL_ROOT="${SCREEN_LOCAL_ROOT:-$HOME/Desktop/mls-intraday/篩選邏輯}"
SCREEN_VPS="${SCREEN_VPS:-mls}"
SCREEN_REMOTE_ROOT="/opt/mls-screen"

test -d "$SCREEN_LOCAL_ROOT"
rsync -avz --delete \
  --exclude='*.db*' --exclude='*.bak*' --exclude='__pycache__/' \
  --exclude='.venv-eod/' --exclude='.DS_Store' \
  --exclude='deploy/source.manifest.sha256' \
  --exclude='backup_candidate_pool_*.json' \
  "$SCREEN_VPS:$SCREEN_REMOTE_ROOT/" "$SCREEN_LOCAL_ROOT/"
printf 'Pulled VPS AB source. Review and commit git changes before deployment.\n'
