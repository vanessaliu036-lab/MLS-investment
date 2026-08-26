#!/usr/bin/env bash
# mls-intraday 一鍵部署到 VPS 66.42.42.150:8000
# 跑法：bash deploy_vps.sh
# 預期：第一次跑會問 SSH host key（輸 yes）、可能問 SSH 密碼

set -euo pipefail

VPS_HOST="66.42.42.150"
VPS_USER="root"
VPS_PORT_SSH="${VPS_SSH_PORT:-22}"
VPS_DEPLOY_DIR="/opt/mls-intraday"
VPS_SCREEN_DIR="/opt/mls-screen"   # AB 引擎(8002)正本，與 8000 站分屬不同目錄
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

# 0. 漂移檢查（擋回捲／擋誤刪，不可略過）
#    這一步存在的理由是兩次真實事故，且兩次都沒有任何錯誤訊息：
#      · ops/mls_aflow_watchdog.py 只存在線上 → 被 --delete 刪掉，空燒兩天沒人發現
#      · screen_post.py 線上版有六態分類／背離救回，repo 沒有 → 部署會靜默抹掉
#    健康檢查一律亮綠，所以只能在推送「之前」擋。
echo "===== 0/6 部署前漂移檢查 ====="
if ! python3 "${LOCAL_SRC}/ops/deploy_guard.py"; then
  if [ "${ALLOW_OVERWRITE:-0}" = "1" ]; then
    echo
    echo "⚠️  ALLOW_OVERWRITE=1：已確認上列線上版本不需保留，繼續部署。"
  else
    echo
    echo "❌ 偵測到漂移，已中止部署。線上有而 repo 沒有的東西，推下去就沒了。"
    echo "   處理方式擇一："
    echo "     · 線上版要保留 → python3 ops/deploy_guard.py --adopt 抓回來 commit"
    echo "     · 確定要退場   → git rm 明確刪除"
    echo "     · 確認過不需保留 → ALLOW_OVERWRITE=1 bash deploy_vps.sh"
    exit 1
  fi
fi

# 1. SSH 連通 + 顯示指紋
echo "===== 1/6 SSH 連通測試 ====="
ssh -p "${VPS_PORT_SSH}" -o StrictHostKeyChecking=accept-new \
    "${VPS_USER}@${VPS_HOST}" "echo ok && uname -a && python3 --version"

# 2. 備份現有部署（cp 不是 mv：原目錄留在原地，資料檔不動、不用還原）
echo "===== 2/6 備份現有部署 ====="
ssh -p "${VPS_PORT_SSH}" "${VPS_USER}@${VPS_HOST}" \
    "if [ -d '${VPS_DEPLOY_DIR}' ]; then cp -a '${VPS_DEPLOY_DIR}' '${VPS_DEPLOY_DIR}.bak.${TIMESTAMP}' && echo 'backup: ${VPS_DEPLOY_DIR}.bak.${TIMESTAMP}'; else echo 'no existing deploy'; fi"

# 3. rsync 源碼（就地更新；資料檔全列排除，rsync --delete 不會刪被排除的檔，
#    所以 mls.db / intraday_eod.db / 快照 / 快取會留在 VPS 原地持續累積）
#    winning_model_backtest/ 是本機獨立的研究用 git repo(自己有 .git/.env)，
#    8000 站程式只在註解裡引用它的凍結文件路徑，執行期不讀取；同步上去只會把
#    160MB 研究資料與它自己的 .env 一起搬進正式站目錄，故排除。
echo "===== 3/6 推送 8000 站源碼 (rsync) ====="
rsync -avz --delete \
  -e "ssh -p ${VPS_PORT_SSH}" \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='*.db' \
  --exclude='*.db-shm' \
  --exclude='*.db-wal' \
  --exclude='*.bak*' \
  --exclude='*.log' \
  --exclude='.DS_Store' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache/' \
  --exclude='.claude/' \
  --exclude='ops/__pycache__' \
  --exclude='live_state.json' \
  --exclude='intraday_live_snapshot.json' \
  --exclude='ma20_cache.json' \
  --exclude='chips_cache.json' \
  --exclude='card_cache/' \
  --exclude='reports/' \
  --exclude='篩選邏輯/' \
  --exclude='winning_model_backtest/' \
  "${LOCAL_SRC}/" \
  "${VPS_USER}@${VPS_HOST}:${VPS_DEPLOY_DIR}/"

# 4. AB 引擎（8002）——正本在 /opt/mls-screen，不在 /opt/mls-intraday 底下。
#    這支腳本長年 --exclude='篩選邏輯/' 且從不碰 /opt/mls-screen，於是「改了引擎、
#    跑了部署、線上沒變」。要「部署就是帶全部新檔案」，這段就不能少。
#    .venv-eod 是線上建的虛擬環境，排除掉（--delete 才不會把它清掉）。
echo "===== 4/6 推送 AB 引擎源碼 → ${VPS_SCREEN_DIR} ====="
ssh -p "${VPS_PORT_SSH}" "${VPS_USER}@${VPS_HOST}" \
    "if [ -d '${VPS_SCREEN_DIR}' ]; then rsync -a --exclude='.venv*' --exclude='__pycache__' \
       '${VPS_SCREEN_DIR}/' '${VPS_SCREEN_DIR}.bak.${TIMESTAMP}/' \
     && echo 'backup: ${VPS_SCREEN_DIR}.bak.${TIMESTAMP} (不含 .venv)'; fi"
rsync -avz --delete \
  -e "ssh -p ${VPS_PORT_SSH}" \
  --exclude='.git' \
  --exclude='.venv*' \
  --exclude='*.db' \
  --exclude='*.db-shm' \
  --exclude='*.db-wal' \
  --exclude='*.bak*' \
  --exclude='bak-*' \
  --exclude='*.log' \
  --exclude='.DS_Store' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache/' \
  --exclude='tests/' \
  --exclude='stage2-status.json' \
  --exclude='backup_*.json' \
  --exclude='deploy/source.manifest.sha256' \
  "${LOCAL_SRC}/篩選邏輯/" \
  "${VPS_USER}@${VPS_HOST}:${VPS_SCREEN_DIR}/"
ssh -p "${VPS_PORT_SSH}" "${VPS_USER}@${VPS_HOST}" \
    "cd ${VPS_SCREEN_DIR} && python3 -m py_compile api.py screen_post.py store.py && systemctl restart mls-ab-engine && sleep 4 && systemctl is-active mls-ab-engine"

# 5. .env 推上去（如有）
echo "===== 5/6 推送 .env ====="
if [ -f "${ENV_FILE}" ]; then
  scp -P "${VPS_PORT_SSH}" "${ENV_FILE}" "${VPS_USER}@${VPS_HOST}:${VPS_DEPLOY_DIR}/.env"
  ssh -p "${VPS_PORT_SSH}" "${VPS_USER}@${VPS_HOST}" "chmod 600 ${VPS_DEPLOY_DIR}/.env && wc -c ${VPS_DEPLOY_DIR}/.env"
  echo "✅ .env 推送完成 (size above, 內容不顯示)"
else
  echo "⚠️  跳過 .env 推送"
fi

# 6. 安裝 deps + 啟動 server
echo "===== 6/6 安裝依賴 + 啟動 server ====="
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

# 7. 驗證
echo "===== 驗證 (6 項) ====="
sleep 2
echo "[1] / 健康"
curl -s -o /dev/null -w "    HTTP %{http_code}\n" "http://${VPS_HOST}:8000/" || true
echo "[2] 盤中資料 API"
curl -s -o /dev/null -w "    HTTP %{http_code}\n" "http://${VPS_HOST}:8000/api/intraday-test" || true
echo "[3] 51 檔股票清單"
# 正確資料端點是 /api/intraday-test，回 {ok, rows:[...]}；舊的 /intraday-test/api/stocks 已 404。
curl -s "http://${VPS_HOST}:8000/api/intraday-test" 2>/dev/null \
  | python3 -c "import json,sys;d=json.load(sys.stdin);rows=d.get('rows',[]);codes=[str(r.get('code')) for r in rows];print(f'    rows count : {len(rows)}');print(f'    first 5    : {codes[:5]}');print(f'    has 8028   : {\"8028\" in codes}')" 2>&1 | head -5
echo "[4] 個股卡片快取（開機預熱）"
ssh -p "${VPS_PORT_SSH}" "${VPS_USER}@${VPS_HOST}" \
  "ls '${VPS_DEPLOY_DIR}/個股卡片相關檔案_20260722/card_cache' 2>/dev/null | wc -l | sed 's/^/    cached cards: /'" || true
echo "[5] UI 標題"
curl -s "http://${VPS_HOST}:8000/" 2>/dev/null | grep -oE "<title>[^<]+</title>" | head -1
echo "[6] 服務日誌最後 5 行（journald）"
ssh -p "${VPS_PORT_SSH}" "${VPS_USER}@${VPS_HOST}" "journalctl -u mls-intraday -n 5 --no-pager -o cat"

echo
echo "===== 部署完成 ====="
echo "UI 網址: http://${VPS_HOST}:8000/"
echo "日誌查詢: ssh ${VPS_USER}@${VPS_HOST} 'journalctl -u mls-intraday -f'"
echo "回滾備份: ${VPS_DEPLOY_DIR}.bak.${TIMESTAMP}"
