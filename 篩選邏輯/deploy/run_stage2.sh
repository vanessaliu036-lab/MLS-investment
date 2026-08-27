#!/usr/bin/env bash
# 盤後第二段（15:05 台北，法人籌碼定案後）：
#   1. 重採 collect（fresh 法人/融資）+ screen_post 二次篩（價量已在 13:35 初篩，本輪加籌碼）
#   2. b_verify（B 鏈法人三態）+ merge_pool（A∪B）→ candidate_pool 定案 = 最終明日觀察名單
#   3. run_pa_snapshot：Pre-Activation 每日快照（2026-08-24 起，live baseline 唯一寫入點）
# 休市日 collect.py / run_stage2_verify 自身會偵測略過，不打任何 API。
#
# ⚠ 為什麼 verify 與 pa 要分開判斷退出碼（不能靠 set -e 一路串）：
#   run_stage2_verify.py 非零退出時，原本 set -e 會直接中斷，**pa 快照被靜默跳過**。
#   快照斷一天，live 樣本就永久缺一天（無法回補：pre_activation 是當下規則的判定）。
#   所以 collect 成功後，verify 與 pa 各跑各的，任一失敗仍以非零收尾交給 stage2_retry 重試。
#   run_pa_snapshot 同日重跑是安全的（INSERT OR REPLACE）。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$APP_DIR"
[ -f "$APP_DIR/.env" ] && set -a && . "$APP_DIR/.env" && set +a
[ -f "$APP_DIR/../.env" ] && set -a && . "$APP_DIR/../.env" && set +a

# collect 失敗 = 當日資料不完整，後面兩步都不該跑（維持原行為）
python3 collect.py

set +e
python3 run_stage2_verify.py; rc_verify=$?
python3 run_pa_snapshot.py;   rc_pa=$?
# Opportunity forward logging(2026-08-24 起)。frozen signal sec_rs_10d@Top10%
# 的最終確認只能靠 8/24 之後的 forward data,每漏一天就少一天乾淨樣本。
python3 run_opportunity_snapshot.py; rc_opp=$?
# funnel.py 快照(2026-08-27 起)。同 run_pa_snapshot.py 的病灶:funnel.run()
# 只從 HTTP handler 呼叫,沒人開 /ab/funnel 當天就沒有 funnel_result,
# reject_verify 的 FNR pipeline 會斷資料。純觀察快照,失敗只告警不擋主流程。
python3 run_funnel_snapshot.py; rc_funnel=$?
set -e

[ "$rc_verify" -ne 0 ] && echo "[stage2] ⚠ run_stage2_verify.py 失敗 rc=$rc_verify" >&2
[ "$rc_pa" -ne 0 ] && echo "[stage2] ⚠ run_pa_snapshot.py 失敗 rc=$rc_pa（Pre-Activation 快照未寫入）" >&2
# Opportunity 是純觀察快照,失敗只告警不擋盤後主流程(rc=2 是資料未齊的合法跳過)
[ "$rc_opp" -ne 0 ] && [ "$rc_opp" -ne 2 ] && echo "[stage2] ⚠ run_opportunity_snapshot.py 失敗 rc=$rc_opp" >&2
[ "$rc_funnel" -ne 0 ] && echo "[stage2] ⚠ run_funnel_snapshot.py 失敗 rc=$rc_funnel（funnel_result 未寫入，FNR pipeline 會斷這天）" >&2
if [ "$rc_verify" -ne 0 ] || [ "$rc_pa" -ne 0 ]; then exit 1; fi
exit 0
