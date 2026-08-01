#!/usr/bin/env bash
# 盤後第二段（15:05 台北，法人籌碼定案後）：
#   1. 重採 collect（fresh 法人/融資）+ screen_post 二次篩（價量已在 13:35 初篩，本輪加籌碼）
#   2. b_verify（B 鏈法人三態）+ merge_pool（A∪B）→ candidate_pool 定案 = 最終明日觀察名單
# 休市日 collect.py / run_stage2_verify 自身會偵測略過，不打任何 API。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$APP_DIR"
[ -f "$APP_DIR/.env" ] && set -a && . "$APP_DIR/.env" && set +a
[ -f "$APP_DIR/../.env" ] && set -a && . "$APP_DIR/../.env" && set +a
python3 collect.py
python3 run_stage2_verify.py
