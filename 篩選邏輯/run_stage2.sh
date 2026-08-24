#!/usr/bin/env bash
# ⚠ 這支只是轉呼叫。正本在 deploy/run_stage2.sh —— systemd（stage2_retry.py）走的是那支。
# 曾經這裡有一份各自維護的副本：少了 run_pa_snapshot.py 那步，且用 exec 收尾，
# 手動補跑時會靜默漏掉 Pre-Activation 快照。改成轉呼叫，永遠只有一份真流程。
set -euo pipefail
exec /usr/bin/env bash "$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/deploy/run_stage2.sh" "$@"
