#!/usr/bin/env bash
# 一鍵回退舊架構:8000 自登入 Shioaji、gateway 停用。
set -uo pipefail
rm -f /etc/systemd/system/mls-intraday.service.d/use-gateway.conf
systemctl daemon-reload
systemctl disable --now mls-quote-gateway.service 2>/dev/null || true
systemctl restart mls-intraday.service
echo "[revert] 已回退舊架構(8000 自登入 Shioaji);gateway 停用。"
