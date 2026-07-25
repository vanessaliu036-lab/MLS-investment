#!/usr/bin/env bash
# 盤中 Shioaji 行情訂閱。由 timer 每週一~五 08:55 觸發。
# 非交易日/非盤中 get_phase()!=INTRADAY → intraday_feed 自身直接退出,Shioaji 一次都不連。
# 收盤(13:30 後)自動停止登出。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$APP_DIR"
[ -f "$APP_DIR/.env" ] && set -a && . "$APP_DIR/.env" && set +a
[ -f "$APP_DIR/../.env" ] && set -a && . "$APP_DIR/../.env" && set +a
exec python3 intraday_feed.py
