#!/usr/bin/env bash
# 盤後採集 + 落地明日盤前名單。由 timer 每週一~五 13:40 觸發。
# 休市日(國定假日)collect.py 自身會偵測並略過,不打任何 API。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$APP_DIR"
# 讀憑證/設定:優先本目錄 .env,再退回專案根 ../.env
[ -f "$APP_DIR/.env" ] && set -a && . "$APP_DIR/.env" && set +a
[ -f "$APP_DIR/../.env" ] && set -a && . "$APP_DIR/../.env" && set +a
PYTHON="python3"
[ -x "$APP_DIR/.venv-eod/bin/python" ] && PYTHON="$APP_DIR/.venv-eod/bin/python"
"$PYTHON" collect.py

# 同一條盤後流程立即完成：A 鏈收盤復盤、B 鏈法人驗證、A/B 匯流。
# 不能只依賴另一個可能未安裝的 stage2 timer，否則 collect 成功但驗證永遠沒有資料。
exec "$PYTHON" run_stage2_verify.py
