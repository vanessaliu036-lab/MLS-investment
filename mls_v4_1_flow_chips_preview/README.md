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

## DB-only source contract

`source_bridge.py` 不假設 VPS 的表名。正式接線時提供明確 SELECT，查詢結果必須含所需欄位；缺一欄直接報錯，不猜欄位。

盤中 snapshot 最低欄位：
`trade_date, symbol, stock_name, ts, high, low, close, prev_close, volume, ma5_volume, vwap, a_flow, net_active, net_flow_amount, price_change_pct, price_data_date, flow_data_time, as_of`。

籌碼最低欄位：
`trade_date, symbol, foreign_net_lots, volume_lots, chip_data_date, as_of`。

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

目前測試涵蓋 Freshness、CLV、Volume Quality、四象限、Rescue、歷史樣本門檻、TOP 排名、連續 flow ticks、A-flow 兩次確認與 DB bridge 欄位驗證。
