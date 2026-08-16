#!/usr/bin/env bash
# 09:10 自動健檢:切換後行情若明顯壞掉(<10/51 有價)自動回退,避免無人盯時盤中沒行情。
set -uo pipefail
[ -f /etc/systemd/system/mls-intraday.service.d/use-gateway.conf ] || exit 0   # 未切換 → 不檢查
N=$(curl -s --max-time 12 "http://127.0.0.1:8000/api/intraday-test" | python3 -c "
import sys,json
try: print(sum(1 for r in json.load(sys.stdin).get(\"rows\",[]) if r.get(\"price\") is not None))
except Exception: print(0)")
if [ "${N:-0}" -lt 10 ]; then
  echo "[autorevert] 切換後只有 ${N}/51 有價 → 明顯壞掉,自動回退" | systemd-cat -t mls-gateway-autorevert
  /opt/mls-intraday/gateway_ops/revert.sh
else
  echo "[autorevert] 切換後 ${N}/51 有價,健康,不動作" | systemd-cat -t mls-gateway-autorevert
fi
