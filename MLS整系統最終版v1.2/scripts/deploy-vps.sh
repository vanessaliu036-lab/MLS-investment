#!/usr/bin/env bash
# Deploy MLS backend to VPS 104.156.239.83
# Run from local repo root:  ./scripts/deploy-vps.sh
set -euo pipefail

VPS_HOST="104.156.239.83"
VPS_USER="root"
REMOTE_DIR="/opt/mls"
SECRETS_DIR="/opt/mls-secrets"

echo "[1/6] rsync repo to ${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}"
rsync -avz --delete \
  --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.vercel' --exclude 'node_modules' \
  --exclude 'reports/' --exclude '*.log' \
  -e "ssh -i ${HOME}/.ssh/id_ed25519 -o StrictHostKeyChecking=no" \
  ./ "${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}/"

echo "[2/6] ensure secrets dir exists on VPS"
ssh -i "${HOME}/.ssh/id_ed25519" -o StrictHostKeyChecking=no "${VPS_USER}@${VPS_HOST}" \
  "mkdir -p ${SECRETS_DIR} && touch ${SECRETS_DIR}/.env && echo '[REMOTE_SECRETS_READY]'"

echo "[3/6] (manual) copy .env to VPS if not done"
echo "  -> scp .env ${VPS_USER}@${VPS_HOST}:${SECRETS_DIR}/.env  (do this once)"

echo "[4/6] docker compose build + up on VPS"
ssh -i "${HOME}/.ssh/id_ed25519" -o StrictHostKeyChecking=no "${VPS_USER}@${VPS_HOST}" \
  "cd ${REMOTE_DIR} && docker compose build --pull && docker compose up -d"

echo "[5/6] wait 15s for healthcheck"
sleep 15
ssh -i "${HOME}/.ssh/id_ed25519" -o StrictHostKeyChecking=no "${VPS_USER}@${VPS_HOST}" \
  "cd ${REMOTE_DIR} && docker compose ps && curl -sS http://127.0.0.1/api/health || echo 'health not ready yet'"

echo "[6/6] verify from local"
curl -sS -o /dev/null -w "HTTP %{http_code} | %{time_total}s\n" \
  http://${VPS_HOST}/api/health || true

echo "[DONE]"
