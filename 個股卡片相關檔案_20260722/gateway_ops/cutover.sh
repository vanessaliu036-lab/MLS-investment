#!/usr/bin/env bash
# Quote Gateway 切換(盤前 08:45–09:00 執行):8000 停止自登入 Shioaji,改由 gateway 當唯一擁有者。
set -euo pipefail
D=/etc/systemd/system/mls-intraday.service.d
mkdir -p "$D"
printf "[Service]\nEnvironment=USE_QUOTE_GATEWAY=1\n" > "$D/use-gateway.conf"
systemctl daemon-reload
systemctl enable --now mls-quote-gateway.service
echo "[cutover] gateway 啟動,等 10s 建立 Shioaji session…"; sleep 10
systemctl restart mls-intraday.service
echo "[cutover] 8000 已重啟(旗標開=改讀 gateway)。等 14s 驗證…"; sleep 14
echo "--- gateway health ---"; curl -s --max-time 10 http://127.0.0.1:8005/health; echo
echo "--- 8000 行情 ---"; curl -s --max-time 12 "http://127.0.0.1:8000/api/intraday-test" | python3 -c "import sys,json;d=json.load(sys.stdin);print(\"有價\",sum(1 for r in d.get(\"rows\",[]) if r.get(\"price\") is not None),\"/51  feed=\",d.get(\"feed_health\",{}).get(\"feed_state\"))"
echo "[cutover] 若上面有價偏低/feed 死 → 立刻跑 revert.sh"
