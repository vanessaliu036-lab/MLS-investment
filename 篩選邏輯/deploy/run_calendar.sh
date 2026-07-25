#!/usr/bin/env bash
# 更新交易日行事曆(TWSE 官方假日 → holidays.json)。由 timer 每週一 06:00 觸發。
# 官方端點一次只回當年度,merge 保留舊年份;跨年後第一個週一就會補上新年度假日。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$APP_DIR"
exec python3 calendar_sync.py
