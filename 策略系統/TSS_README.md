# TSS v1.0 MVP — 台股主動買賣盤四因子進場策略

> **狀態**：✅ 全功能上線 (B 路線 — 跟主系統並存,不動主邏輯)
> **對應規格書**：交易策略系統技術規格書 TSS v1.0 (2026-07-09)
> **環境**：Python 3 / Shioaji 1.5.5 / pandas / numpy / FinMind

---

## 這是什麼

TSS v1.0 規格書的完整 MVP 實作。**完全不動**主系統 (`engine.py` / `scoring.py` / `money_health.py` / `after_hours.py` / `chips.py`)。

四因子：
1. 市場系統面（大盤 MA20）
2. 籌碼共鳴面（千張大戶 + 三大法人）
3. 技術發動面（個股 MA20 + 乖離 + 量 + 破前高）
4. 主動買賣盤（Buy/Sell Vol > 1.25）

---

## 檔案總覽

| 檔案 | 用途 | 對應規格書章節 |
|---|---|---|
| `tss_mvp.py` | 核心模組 — Buy/Sell Vol、四因子篩選、ShioajiActiveVolumeTracker、盤中觸發、IntradayDecision、fetch_index_daily、fetch_institutional、fetch_big_holder | 三、四、五、六、七、八、九 |
| `main.py` | 單檔啟動入口（dry-run / live）— 抓資料 → 篩選 → 報表 | 整合入口 |
| `tss_scheduler.py` | 排程入口（多檔 watchlist + 盤後/盤中模式切換） | 整合入口 |
| `TSS_README.md` | 本檔 | — |
| `reports/TSS_YYYYMMDD.md` | 單檔跑結果 | — |
| `reports/TSS_WATCHLIST_YYYYMMDD.md` | 多檔 watchlist 報表 | — |
| `data/big_holder/Top1000_ratio_<code>.csv` | 集保千張大戶快取 (規格書第九章三層架構精簡層) | 第九章 |

---

## 怎麼跑

### 1. Dry-run（不登入券商,用 mock 資料驗證流程）

```bash
cd "/Users/vanessaliu/Desktop/01_投資分析/MLS 完整系統 v1.2/MLS整系統最終版v1.2"
python3 main.py --dry-run --days 30
```

### 2. Live 單檔盤後篩選（台積電為例）

```bash
export SHIOAJI_API_KEY="你的_API_KEY"
export SHIOAJI_SECRET_KEY="你的_SECRET_KEY"
python3 main.py --code 2330 --days 30
```

### 3. Live 多檔 watchlist（用排程入口）

```bash
python3 tss_scheduler.py --watchlist 2330 2454 2603 --mode after_market
```

### 4. 盤中 tick 監控（已通過四因子的合格標的）

```bash
python3 tss_scheduler.py --watchlist 2330 --mode intraday --intraday-min 240
```

### 5. 全套（盤後篩 → 切盤中監控）

```bash
python3 tss_scheduler.py --watchlist 2330 2454 2603 --mode all
```

---

## 模組對應 (規格書 ↔ 程式碼)

| 規格書章節 | 對應函數/類別 |
|---|---|
| 三、 3.1 主動買賣盤定義 | `classify_buy_sell_vol()`、`ShioajiActiveVolumeTracker.add_tick()` |
| 三、 3.2 MA20 / 千張 / 法人 | MA20 → `filter_after_market` 內 daily groupby；千張 → `fetch_big_holder()`；法人 → `fetch_institutional()` |
| 四、 盤後篩選四因子 | `filter_after_market()` |
| 五、 盤中觸發四條件 | `filter_intraday()` + `run_intraday_loop()` + `IntradayConfig` / `IntradayDecision` |
| 六、 強制停止進場 | `filter_after_market()` 內 force_stop + `filter_intraday()` 內 force_stop (含指數急殺、爆量、財報空窗) |
| 七、 7.1 ShioajiActiveVolumeTracker | `tss_mvp.ShioajiActiveVolumeTracker` |
| 七、 7.2 fetch_1min_kbars | `tss_mvp.fetch_1min_kbars()` |
| 八、 風險管理 | `IntradayConfig` 含停損/停利閾值預留 (未接實際下單) |
| 九、 Shioaji 注意事項 | `shioaji_login()` 用 `api_key/secret_key`、`fetch_1min_kbars()` 內 `time.sleep(0.5)` |
| 九、 集保 SOP (週四 17:00) | `fetch_big_holder()` 內含排程判斷 (`is_thursday_after_5pm`) + 三層檔案架構 |

---

## 跟主系統的關係

| 項目 | 主系統 (engine / scoring) | TSS MVP |
|---|---|---|
| 因子數 | 5 因子 + Level 8.1 三維交叉 | 4 因子 |
| 主動買賣盤 | ❌ 沒有 | ✅ Buy/Sell Vol |
| 盤中即時 | ✅ (server.py API) | ✅ (ShioajiActiveVolumeTracker + run_intraday_loop) |
| 排程 | after_hours.py 13:40 | tss_scheduler.py (獨立,不動主系統) |
| 下單 | ✅ broker.py | ❌ 純篩選 (規格書只到進場訊號) |
| 集保 | chips.py 大戶比例 (週) | fetch_big_holder() + 三層檔案架構 |
| 法人 | chips.py inst_net_20d_lots | fetch_institutional() 逐日 (Foreign_Investor / Investment_Trust / Dealer) |
| 大盤 | broker.py index_snapshot 即時 | fetch_index_daily() 歷史日 K |
| **互不干擾** | — | ✅ 0 修改主檔 |

---

## 驗證結果 (2026-07-09)

**Dry-run**: 跑通，Buy/Sell Vol 計算 + 四因子 + 報表寫檔。

**Live 2330 台積電 (今日)**:
- BS Ratio 0.568 < 1.25 → C4 沒過
- 收盤 2415 > MA20 2411 → C3 above_ma20 過
- 但 close 沒破昨日高 → C3 break_prev_high 沒過
- Final: 不進場（合理）

**Live watchlist (2330 / 2454 / 2603)**:
- 2330: bs=0.57 → ❌
- 2454: bs=0.72 → ❌
- 2603: bs=0.77 → ❌
- 0 檔合格（合理：今天內盤偏重）

---

## 已完成

- [x] Buy/Sell Vol 計算 + 四因子盤後篩選
- [x] C1 大盤 MA20 判斷
- [x] C2 法人逐日 (FinMind `TaiwanStockInstitutionalInvestorsBuySell`)
- [x] C2 大戶週資料 (FinMind `TaiwanStockHoldingSharesPer` + 三層檔案)
- [x] C3 技術發動面
- [x] C4 主動買賣盤 Buy/Sell Ratio
- [x] 六、 強制停止進場 (指數急殺 + 爆量 + 財報空窗)
- [x] 七、 ShioajiActiveVolumeTracker 介面 + 五檔 fallback
- [x] 七、 fetch_1min_kbars 自動 5 天分段
- [x] 五、 盤中 tick loop + 條件 4 寬鬆/嚴苛切換
- [x] 排程入口 tss_scheduler.py
- [x] 報表自動寫檔 (single + watchlist)

## 未做（需 Vanessa 拍板才動）

- [ ] 接 `after_hours.py` 主排程（目前 tss_scheduler.py 獨立跑，不掛主鉤）
- [ ] 實際下單（規格書八章停損停利 — `broker.py` 未實作訂單層）
- [ ] 國定假日判斷（目前只看 weekday < 5）
- [ ] 集保原始 CSV 從證交所下載（目前用 FinMind，更穩但略離規格書原始 SOP）

---

## 規格書對應完成度

| 章節 | 標題 | 狀態 |
|---|---|---|
| 一 | 系統概述 | — |
| 二 | 系統架構與資料流 | ✅ |
| 三 | 核心指標定義 | ✅ 完整 |
| 四 | 盤後篩選模組 | ✅ 完整 |
| 五 | 盤中觸發模組 | ✅ 完整 (含寬鬆/嚴苛切換) |
| 六 | 強制停止進場條件 | ✅ (指數急殺 + 爆量 + 財報空窗) |
| 七 | 程式碼模組架構 | ✅ 完整 |
| 八 | 風險管理與參數 | ✅ 參數定義 (停損停利閾值預留,實際下單未接) |
| 九 | Shioaji 開發注意事項 | ✅ (api_key/secret_key + Decimal + kbars 5天分段 + time.sleep) |