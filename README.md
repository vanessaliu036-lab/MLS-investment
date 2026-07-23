# MLS v4.0 盤中篩選邏輯模組

盤中個股篩選公式核心 + 訂閱骨架。所有公式為純函式，可逐項驗算（對齊「健康分可逐項驗算」慣例）。

## 檔案

| 檔案 | 職責 |
|---|---|
| `app/intraday_filter.py` | **篩選公式核心**：aflow 兩種算法、代理象限、篩選條件、雙軌狀態機 |
| `app/market.py` | 族群彙總：溫度計、族群熱圖、四象限分布 |
| `app/intraday.py` | 訂閱骨架：雙層 subscribe、心跳偵測重連、buffer 落地 stock_state |
| `tests/test_filter.py` | 公式逐項驗算（含 6/17 被動元件正例、華邦電軌道隔離） |

## 最高鐵律

盤中即時資料**一律用訂閱**（`api.subscribe`），推播不計流量。
**嚴禁盤中輪詢** `snapshots` / `ticks` / `kbars` —— 超限會回**空值**，
這正是「資金流一直抓不到」的最常見主因。

## 主動買賣差 aflow — 兩種算法對照

| 算法 | 公式 | 用途 |
|---|---|---|
| A 官方現成 | `ask_side_total_vol − bid_side_total_vol` | 主用，最穩 |
| B 逐筆累加 | Σ(外盤1:+vol, 內盤2:−vol, 無法判定0:不計) | 對照偵錯 |

`aflow_reconcile(A, B)` 背離超容忍 → `diverged=True` → UI 標「校驗背離，疑訂閱異常」。
兩算法背離是斷線 / 漏筆的早期警訊。

**鐵律**：aflow 是買賣盤積極度估算，**非法人淨買賣**；盤中流出 **≠ 派發**。

## 篩選條件（UI「符合條件」模式對應 `FILTER_RULES`）

| key | 條件 | 公式 |
|---|---|---|
| `aflow_positive` | 主動差 > 0 | `aflow > 0` |
| `above_ma20` | 站上月線 | `dist_ma20 > 0`（MA20 盤前算快取） |
| `quadrant_attack` | 代理象限＝真攻擊 | `aflow>0 且 漲` |

`passes_filters(snap)` 回傳逐條結果 + `all_pass`。UI 全名單模式全顯示；符合條件模式只顯示 `all_pass=True`。

## 代理象限四格

| | 漲 | 跌 |
|---|---|---|
| aflow>0 | 真攻擊 | 惜售（低接） |
| aflow<0 | 假紅 | 休息 |

代理值會隨盤變，UI 必標「代理·未定案」，收盤定案。
6/17 開盤壓吸階段代理象限如實顯示流出，人工靠鐵律判讀為吸籌——公式不自作聰明改判。

## 雙軌狀態機（v3.0 playbook，華邦電教訓：軌道不可混）

**引擎軌**：站上 MA20 + 昨日法人連買 → 訊號成立；跌破 MA20 → 停損
**攻擊軌**：突破當日觸發價 → 訊號成立；跌破 ATR 停損 → 停損

`next_state(prev, snap)` 依 `snap.track` 嚴格分流，攻擊軌不套引擎軌 swing 邏輯。

## 盤中欄位紀律

| 欄位 | 盤中 |
|---|---|
| 即時價 / 漲跌% / aflow / 五檔 / 量比 | ✅ 訂閱算得到 |
| 離 MA20 / 觸發價 / ATR 停損 | ✅ 盤前算好快取 |
| 象限 / 健康分 | ⚠️ 代理·未定案 |
| 外資 / 投信 / 融資 / 承接品質★ / 法人連買 | ❌ **一律昨日盤後值** |

## 接盤後系統（不重寫，直接 import）

```python
import broker, decision, db, config     # 盤後既有模組
from app import intraday, intraday_filter, market

intraday.subscribe_all(api, config.UNIVERSE, watch_pool)
# APScheduler interval 3–5 秒，09:00–13:30 收盤停：
result = intraday.tick(api, config.UNIVERSE, watch_pool, meta_of, prefetch_of, db)
```

- `prefetch_of(code)`：盤前算好 `ma20/trigger_price/atr_stop/yesterday_volume` +
  昨日底本 `inst_buy_days`（`db.load_dec_health(昨日)`）
- 累積量每輪落地 `stock_state`，崩潰重啟從 DB 重播不歸零
- 13:30 收盤凍結盤中狀態 → 觸發 `after_hours.run_eod()`

## 驗算

```bash
python tests/test_filter.py        # 16 項逐項驗算
```
