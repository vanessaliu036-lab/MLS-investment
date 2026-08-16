# Quote Gateway 切換 Runbook

**目的**：把 Shioaji 從 8000 網頁行程抽離，改由 `mls-quote-gateway.service` 當**唯一擁有者**，
根治「同金鑰多重登入互踢 session → 盤中畫面死」。單一金鑰即可（方案 B：gateway 也代理
kbar/大盤/掃描，8000 完全不登入）。

## 架構（旗標開時）
```
Shioaji ─┐
         ├─ mls-quote-gateway(8005 + /dev/shm 快照) ── 8000 broker(HTTP 代理) ── 前端/feed_bridge
TWSE MIS ┘   (唯一登入者;kbar/index/scan/raw_buffer 全在這)
```
- 旗標 `USE_QUOTE_GATEWAY=1`（mls-intraday.service 的 systemd drop-in）→ 8000 的 `broker.py`
  8 個函式短路成打 gateway 8005，`ensure_subscribed`/`get_api` 不登入。
- gateway 自身在 `quote_gateway.py` 頂端強制 `USE_QUOTE_GATEWAY=0`，永遠用真 Shioaji。
- **旗標關（現況）= 上述全部失效、行為與改版前完全相同**（已驗 `_use_gw()=False`）。

## 為什麼只能盤前切
gateway 驗證登入必須在**市場時段當唯一擁有者**，而那當下 8000 也持有 session。切換本身就是
「關 8000 登入 → gateway 登入 → 看首筆 tick」，**只能在開盤那一刻做才有 tick 即時驗**。
盤中切 = 拿正在成交的盤去賭；收盤切 = gateway 收盤不碰 Shioaji、無法驗登入。

## 切換步驟（**08:45–09:00 台北**，人在場）
```bash
ssh mls '/opt/mls-intraday/gateway_ops/cutover.sh'
```
腳本會：建旗標 drop-in → 啟用+啟動 gateway → 重啟 8000 → 印 gateway health + 8000 有價數。
**看輸出**：`有價 ≈51/51、feed=LIVE` = 成功；偏低/feed 死 = 立刻回退。

## 一鍵回退（切壞或想退）
```bash
ssh mls '/opt/mls-intraday/gateway_ops/revert.sh'
```
移除旗標 → 停用 gateway → 重啟 8000（回舊架構自登入）。

## 自動保險
`mls-gateway-autorevert.timer` 每交易日 **09:10** 健檢：若已切換且 8000 有價 `<10/51`
（明顯壞掉）→ 自動跑 `revert.sh`。未切換則跳過。日誌：`journalctl -t mls-gateway-autorevert`。

## 切換後驗證清單
- `curl 127.0.0.1:8005/health` → feed_state=LIVE、last_tick_age 小、counts.quote_live 高
- `curl 127.0.0.1:8000/api/intraday-test` → 有價 ≈51、aflow 有真值
- 個股卡片能開（驗 kbar 代理）、大盤廣度有數（驗 index 代理）
- `journalctl -u mls-quote-gateway -f` → 有 tick、無 login 500
- feed_bridge 照常（它讀 8000，自動吃到 gateway 資料，無需改）

## 已知前提
- 只有一把 Shioaji 金鑰；方案 B 用單鑰（gateway 獨佔）成立。
- gateway systemd 需 `HOME=/root`（token pool），已在 drop-in `home.conf` 設好。
