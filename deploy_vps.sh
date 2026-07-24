#!/usr/bin/env bash
# mls-intraday 一鍵部署到 VPS 66.42.42.150:8000
# 跑法：bash deploy_vps.sh
# 預期：第一次跑會問 SSH host key（輸 yes）、可能問 SSH 密碼

set -euo pipefail

VPS_HOST="66.42.42.150"
VPS_USER="root"
VPS_PORT_SSH="${VPS_SSH_PORT:-22}"
VPS_DEPLOY_DIR="/opt/mls-intraday"
LOCAL_SRC="$HOME/Desktop/mls-intraday"
ENV_FILE="$LOCAL_SRC/.env"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

echo "===== mls-intraday deploy to ${VPS_USER}@${VPS_HOST} ====="
echo "local src : ${LOCAL_SRC}"
echo "vps dest  : ${VPS_DEPLOY_DIR}"
echo "timestamp : ${TIMESTAMP}"
echo

# 0. 前置檢查
if [ ! -d "${LOCAL_SRC}" ]; then
  echo "❌ 本機源碼不存在: ${LOCAL_SRC}"
  exit 1
fi
if [ ! -f "${ENV_FILE}" ]; then
  echo "⚠️  本機 .env 不存在 (${ENV_FILE})，部署後 server 將無 Shioaji 金鑰"
  echo "    按 Enter 繼續、或 Ctrl+C 中止先去建 .env"
  read -r _
fi

# 1. SSH 連通 + 顯示指紋
echo "===== 1/5 SSH 連通測試 ====="
ssh -p "${VPS_PORT_SSH}" -o StrictHostKeyChecking=accept-new \
    "${VPS_USER}@${VPS_HOST}" "echo ok && uname -a && python3 --version"

# 2. 備份現有部署（cp 不是 mv：原目錄留在原地，資料檔不動、不用還原）
echo "===== 2/5 備份現有部署 ====="
ssh -p "${VPS_PORT_SSH}" "${VPS_USER}@${VPS_HOST}" \
    "if [ -d '${VPS_DEPLOY_DIR}' ]; then cp -a '${VPS_DEPLOY_DIR}' '${VPS_DEPLOY_DIR}.bak.${TIMESTAMP}' && echo 'backup: ${VPS_DEPLOY_DIR}.bak.${TIMESTAMP}'; else echo 'no existing deploy'; fi"

# 3. rsync 源碼（就地更新；資料檔全列排除，rsync --delete 不會刪被排除的檔，
#    所以 mls.db / intraday_eod.db / 快照 / 快取會留在 VPS 原地持續累積）
echo "===== 3/5 推送源碼 (rsync) ====="
rsync -avz --delete \
  -e "ssh -p ${VPS_PORT_SSH}" \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='*.db' \
  --exclude='*.bak' \
  --exclude='*.log' \
  --exclude='.DS_Store' \
  --exclude='__pycache__' \
  --exclude='live_state.json' \
  --exclude='intraday_live_snapshot.json' \
  --exclude='ma20_cache.json' \
  --exclude='chips_cache.json' \
  --exclude='card_cache/' \
  --exclude='reports/' \
  "${LOCAL_SRC}/" \
  "${VPS_USER}@${VPS_HOST}:${VPS_DEPLOY_DIR}/"

# 4. .env 推上去（如有）
echo "===== 4/5 推送 .env ====="
if [ -f "${ENV_FILE}" ]; then
  scp -P "${VPS_PORT_SSH}" "${ENV_FILE}" "${VPS_USER}@${VPS_HOST}:${VPS_DEPLOY_DIR}/.env"
  ssh -p "${VPS_PORT_SSH}" "${VPS_USER}@${VPS_HOST}" "chmod 600 ${VPS_DEPLOY_DIR}/.env && wc -c ${VPS_DEPLOY_DIR}/.env"
  echo "✅ .env 推送完成 (size above, 內容不顯示)"
else
  echo "⚠️  跳過 .env 推送"
fi

# 5. 安裝 deps + 啟動 server
echo "===== 5/5 安裝依賴 + 啟動 server ====="
ssh -p "${VPS_PORT_SSH}" "${VPS_USER}@${VPS_HOST}" bash <<EOSSH
set -e
cd ${VPS_DEPLOY_DIR}/個股卡片相關檔案_20260722
pip3 install --quiet --break-system-packages shioaji fastapi 'uvicorn[standard]' pandas python-dotenv 2>&1 | tail -3 || true
# 正式站由 systemd 管理(Restart=always)，不可 pkill；統一走 systemctl
systemctl restart mls-intraday
sleep 3
systemctl is-active mls-intraday
echo "--- journalctl last 20 lines ---"
journalctl -u mls-intraday -n 20 --no-pager
EOSSH

# 6. 驗證
echo "===== 6/6 驗證 (6 項) ====="
sleep 2
echo "[1] / 健康"
curl -s -o /dev/null -w "    HTTP %{http_code}\n" "http://${VPS_HOST}:8000/" || true
echo "[2] /intraday-test 路由"
curl -s -o /dev/null -w "    HTTP %{http_code}\n" "http://${VPS_HOST}:8000/intraday-test" || true
echo "[3] 51 檔股票清單"
curl -s "http://${VPS_HOST}:8000/intraday-test/api/stocks" 2>/dev/null \
  | python3 -c "import json,sys;d=json.load(sys.stdin);codes=d.get('codes',d);print(f'    codes count: {len(codes)}');print(f'    first 5    : {codes[:5]}');print(f'    has 8028   : {8028 in [str(c) for c in codes]}')" 2>&1 | head -5
echo "[4] 引擎成員"
curl -s "http://${VPS_HOST}:8000/intraday-test/api/stocks" 2>/dev/null \
  | python3 -c "import json,sys;d=json.load(sys.stdin);eng=d.get('engine_stocks',[]);print(f'    engine: {sorted(eng)}')" 2>&1 | head -3
echo "[5] UI 標題"
curl -s "http://${VPS_HOST}:8000/intraday-test/" 2>/dev/null | grep -oE "<title>[^<]+</title>" | head -1
echo "[6] log 最後 5 行"
ssh -p "${VPS_PORT_SSH}" "${VPS_USER}@${VPS_HOST}" "tail -5 /tmp/mls-intraday.log"

echo
echo "===== 部署完成 ====="
echo "UI 網址: http://${VPS_HOST}:8000/intraday-test"
echo "日誌查詢: ssh ${VPS_USER}@${VPS_HOST} 'tail -f /tmp/mls-intraday.log'"
echo "回滾備份: ${VPS_DEPLOY_DIR}.bak.${TIMESTAMP}"
