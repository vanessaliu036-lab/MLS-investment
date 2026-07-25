#!/usr/bin/env bash
# 盤後採集 + 落地明日盤前名單。由 timer 每週一~五 13:40 觸發。
# 休市日(國定假日)collect.py 自身會偵測並略過,不打任何 API。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$APP_DIR"
# 讀憑證/設定:優先本目錄 .env,再退回專案根 ../.env
[ -f "$APP_DIR/.env" ] && set -a && . "$APP_DIR/.env" && set +a
[ -f "$APP_DIR/../.env" ] && set -a && . "$APP_DIR/../.env" && set +a
exec python3 collect.py
