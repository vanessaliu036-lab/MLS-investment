#!/usr/bin/env bash
# Canonical AB engine deployment: local 篩選邏輯/ -> VPS /opt/mls-screen.
# Refuses to overwrite server-side drift. Use pull_screen_vps.sh first when
# deliberate online edits must be adopted into git.
set -euo pipefail

SCREEN_LOCAL_ROOT="${SCREEN_LOCAL_ROOT:-$HOME/Desktop/mls-intraday/篩選邏輯}"
SCREEN_VPS="${SCREEN_VPS:-mls}"
SCREEN_REMOTE_ROOT="/opt/mls-screen"
SCREEN_MANIFEST="$SCREEN_REMOTE_ROOT/deploy/source.manifest.sha256"

test -d "$SCREEN_LOCAL_ROOT"
ssh "$SCREEN_VPS" "$SCREEN_REMOTE_ROOT/deploy/sync_guard.sh $SCREEN_REMOTE_ROOT verify"

screen_stage_dir="$(mktemp -d)"
trap 'rm -rf "$screen_stage_dir"' EXIT
rsync -a --delete \
  --exclude='*.db*' --exclude='*.bak*' --exclude='__pycache__/' \
  --exclude='.venv-eod/' --exclude='.DS_Store' \
  --exclude='deploy/source.manifest.sha256' \
  --exclude='backup_candidate_pool_*.json' \
  "$SCREEN_LOCAL_ROOT/" "$screen_stage_dir/"

python3 -m compileall -q "$screen_stage_dir"
rsync -avz --delete \
  --exclude='*.db*' --exclude='*.bak*' --exclude='__pycache__/' \
  --exclude='.venv-eod/' --exclude='.DS_Store' \
  --exclude='deploy/source.manifest.sha256' \
  --exclude='backup_candidate_pool_*.json' \
  "$screen_stage_dir/" "$SCREEN_VPS:$SCREEN_REMOTE_ROOT/"

# Record the just-uploaded canonical source before restart.  If startup fails,
# the next corrective deployment must not be mistaken for an unexplained VPS
# hand edit and blocked by the drift guard.
ssh "$SCREEN_VPS" "$SCREEN_REMOTE_ROOT/deploy/sync_guard.sh $SCREEN_REMOTE_ROOT snapshot"

ssh "$SCREEN_VPS" "systemctl restart mls-ab-engine.service; \
  n=0; until curl -fsS --max-time 3 http://127.0.0.1:8002/api/health >/dev/null; do \
    n=\$((n+1)); [ \"\$n\" -ge 90 ] && { \
      systemctl status mls-ab-engine.service --no-pager -l; \
      journalctl -u mls-ab-engine.service -n 80 --no-pager -o cat; exit 1; }; \
    sleep 1; \
  done"

printf 'AB engine deployed and verified: %s -> %s:%s\n' \
  "$SCREEN_LOCAL_ROOT" "$SCREEN_VPS" "$SCREEN_REMOTE_ROOT"
