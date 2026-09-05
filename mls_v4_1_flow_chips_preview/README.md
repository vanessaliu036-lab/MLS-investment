# MLS v4.1 Flow × Chips Preview

獨立旁掛預覽版。目的：把「盤中資金流入/流出 TOP10 + 最近籌碼 + v4.1 錯殺救援判斷 + 歷史相似情境勝率」寫成可回測的程式，而不是靠人工對話判讀。

## 隔離保證

- 不修改既有 `engine.py / scoring.py / chips.py / db.py / server.py`。
- 不 ALTER、UPDATE、DELETE 既有 MLS 資料表。
- 本階段不連 VPS，不掛正式站路由。
- 預覽服務使用自己的 SQLite DB；正式接線時只透過 `source_bridge.py` 以 read-only SELECT 讀既有 DB。
- 資料缺欄或日期錯位時輸出 `NO_DATA / STALE_PRICE_DATA`，禁止使用舊值 fallback。

## 已實作的 v4.1 規則

1. Data Freshness Gate：`price_data_date != trade_date` → `STALE_PRICE_DATA` → `OBSERVE_ONLY`。
2. CLV：振幅 < 2.5% 標 `LOW_CONFIDENCE`，改用 `close > VWAP && close > prev_close`。
3. Volume Quality 2×2：`SHAKEOUT / HEAVY_ABSORPTION / NATURAL_DECAY / FLOW_PRICE_DIVERGENCE`。
4. Persistent Flow：`foreign_net_4d / volume_4d`，不以絕對張數作跨股強度比較。
5. False-Fail Rescue：`TRUE_FAIL / FALSE_FAIL_RESCUE / FALSE_FAIL_RESCUE_HIGH`。
6. Triggered-but-Rejected：Trigger 通過但 CLV < .40 且量 > 1.5× → 降級。
7. Regime Gate：RISK_OFF 下 Rescue 暫停操作權重。
8. Rescue Validation：RISK_ON 與 RISK_OFF 各需 n>=60 且相對 baseline +15pp，否則 Rescue 只顯示觀察。
9. 歷史勝率：n<20 不顯示百分比，只顯示「樣本不足」。

## Plugin DB schema

- `intraday_snapshot`: 5 分鐘 snapshot，可保存 51 檔全池。
- `chip_daily`: 每日外資/法人/成交量與大戶趨勢。
- `trigger_context`: 原 Trigger / monitor price / pass-fail 狀態。
- `market_regime_daily`: RISK_ON/RISK_OFF 與 baseline。
- `flow_threshold_config`: 既有「站上設定金額」門檻；支援 `symbol:股票代號` override，再 fallback `default`。
- `decision_history`: 相似情境勝率、+3/+5、MFE/MAE、baseline。
- `false_kill_kpi`: 錯殺率與 freshness pass rate。
- `reversal_day1_history`: 反轉 Day-1 首次訊號、Persistence、T+1/T+2/T+3、3 日 MFE/MAE。

## DB-only source contract

`source_bridge.py` 不假設 VPS 的表名。正式接線時提供明確 SELECT，查詢結果必須含所需欄位；缺一欄直接報錯，不猜欄位。

盤中 snapshot 最低欄位：
`trade_date, symbol, stock_name, ts, high, low, close, prev_close, volume, ma5_volume, vwap, a_flow, net_active, net_flow_amount, price_change_pct, price_data_date, flow_data_time, as_of`。

籌碼最低欄位：
`trade_date, symbol, foreign_net_lots, institutional_net_lots, volume_lots, chip_data_date, as_of`。

## Extreme Outflow → REVERSAL DAY-1 研究軌

這條軌與 `False-Fail Rescue` 完全分離，也不依賴 C1+C2 是否把股票留在前一日候選池。它直接掃 plugin DB 的全體盤中 snapshot，專門抓「前期籌碼很差，因此舊系統根本沒監控；但 Day-1 早盤資金突然翻正」的 False Negative。

狀態：

- `OUTFLOW_REVERSAL_WATCH`：近 5D 或 20D 法人標準化 flow 為負，先保留。
- `REVERSAL_DAY1_EARLY`：R1 前期流出 + R2 漲幅至少 +1.5% + R3 A-flow > 0 + 站上 VWAP。這一層故意不等 30–90 分鐘，避免漲到尾段才發現。
- `REVERSAL_PRIORITY`：EARLY 之後，再確認 R4 A-flow 在 30–90 分鐘持續增加、R5 同期間價格同步墊高。

R1–R5：

1. 前期流出：5D / 20D institutional net flow 以成交量標準化，不直接比較絕對張數。
2. Day-1 價格反轉：預覽門檻 +1.5%（研究參數，後續依樣本校準）。
3. A-flow Flip：當前 A-flow > 0；跨日意義為「昨日籌碼負 → 今日主動流正」。
4. A-flow Persistence：找 30–90 分鐘前最近一筆 snapshot，要求當時 A-flow > 0 且最新 A-flow 更高。
5. Price Confirmation：同一 Persistence window 價格同步提高，且目前仍在 VWAP 上方。

此軌目前固定 `OBSERVE_ONLY`。即使狀態為 `REVERSAL_PRIORITY` 也不取得正式 ACTION 權限，先累積 T+1 / T+2 / T+3、MFE、MAE 與 regime 對照。

## 本機預覽

```bash
cd mls_v4_1_flow_chips_preview
python -m mls_v4_1_flow_chips.demo_seed demo.db
MLS_FLOW_CHIPS_DB=demo.db python -m mls_v4_1_flow_chips.preview_server
# http://127.0.0.1:8011
```

`demo.db` 只用來看 UI/狀態機，不代表真實 2026/09/01 行情。

## 測試

```bash
PYTHONPATH=. pytest -q
```

測試涵蓋 Freshness、CLV、Volume Quality、四象限、Rescue、歷史樣本門檻、TOP 排名、連續 flow ticks、A-flow 兩次確認、DB bridge 欄位驗證，以及 8150/3532/3374 類型的 REVERSAL DAY-1 / Persistence 對照。

