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
| A 官方現成 | `bid_side_total_vol − ask_side_total_vol` | 主用，最穩 |
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

---

## 更新：aflow 強度佔比篩選（解決「惜售太多、分不出潛力股」）

大跌盤裡幾乎每檔都有人低接，象限「惜售」大量出現、失去鑑別度。
aflow 是**絕對金額**，分不出「普遍低接」與「異常強吸籌」。新增第四條篩選：

**`aflow_intensity = aflow / 成交量 × 100%`**，門檻 `AFLOW_INTENSITY_MIN = 10.0`（可調）。

| 股票 | aflow | 量 | 佔比 | 10%門檻 |
|---|---|---|---|---|
| 安碁 6174 | 136 | 368 | **37.0%** | ✓ 被埋沒的潛力股 |
| 尼克森 3317 | 234 | 1750 | 13.4% | ✓ |
| 加高 8182 | 119 | 1059 | 11.2% | ✓ |
| 台光電 2383 | 97 | 2237 | 4.3% | ✗ 普遍低接 |

安碁 aflow 絕對值不是最大，但佔比 37% 遠超眾人 → 靠強度篩選揪出。

**排序 `rank_potential()`**：主排強度佔比（吸籌強度），次排抗跌（跌幅小者優先）。
抗跌只當「同強度時的排序依據」，**不當硬門檻**——避免大跌盤把最強吸籌股（如安碁 −8.25%）刷掉。

驗證：`test_screenshot_only_gems_pass` 用截圖 12 檔實跑，10% 門檻只留安碁/尼克森/加高。

---

## 更新：盤勢模式自動切換（解決「下跌盤真攻擊永遠不亮」）

問題：截圖顯示整頁清一色「惜售」（大跌盤大單承接），「象限真攻擊」要求漲+流入，
下跌盤永遠不亮 → all_pass 掛零 → 篩不出股。

解法：象限篩選改為**依盤勢自動切換**，同一時間只套用適合當下的那條。

| 盤勢 | 溫度計 score | 象限篩選要求 | 劇本 |
|---|---|---|---|
| 攻擊盤 | >60 | 真攻擊（漲+流入） | 追漲突破 |
| 防守盤 | <40 | **強惜售**（跌+大單接+吸籌強度足） | 抄跌深反彈 |
| 震盪盤 | 40–60 | 真攻擊 或 強惜售 | 兩者皆可 |

- 盤勢由既有溫度計 `score` 自動判：`market_regime(score)`
- **強惜售 ≠ 單純惜售**：下跌盤惜售人人有，必須疊加 aflow 強度佔比才算「強」，
  才能從一片惜售中揪出昇陽半導體(+1077/7203)、欣興(+873)這種真承接股，
  排除旺矽(+2/694=0.3%)這種弱惜售。
- 真攻擊與強惜售是**兩套互斥 playbook**（追漲 vs 抄底），不合併——守華邦電教訓「玩法不混」。

用法：`passes_filters(snap, regime=F.market_regime(thermo["score"]))`。
不給 regime 則沿用舊固定真攻擊（向後相容）。`tick()` 已自動接溫度計判盤勢。

驗證：`test_passes_filters_defense_regime` 證昇陽半導體防守盤 all_pass、攻擊盤被卡死。

---

## v4 更新：三態邏輯 + MA20 接入 + 極端價防護

### 1. MA20 接入（引擎軌命脈，本系統自帶）
`prefetch_ma20.py`：盤前用 `api.kbars()` 抓近 20 日日線算 MA20 快取，盤中即時價直接比。
- 只盤前跑一次（kbars 是查詢型，嚴禁盤中輪詢）。
- `build_ma20_cache(api, universe)` → `{code: ma20}`，餵進 `prefetch_of` 的 `ma20`。
- 資料不足（新股）→ None → 該檔判 NO_DATA，不補造。
- 缺 MA20 的實際影響：引擎軌 100% 癱（進場/停損失效）+ 篩選少一條。五檔缺只影響型態判讀精細度，不影響決策，故本版先補 MA20。

### 2. 三態邏輯 PASS / FAIL / NO_DATA
把「算不出」和「不通過」分開，杜絕「✗ 誤導」：

| 情況 | 舊（二元） | 新（三態） | 顯示 |
|---|---|---|---|
| 站上月線 | ✓ | PASS | ✓站上MA20 |
| 跌破月線 | ✗ | FAIL | ✗站上MA20 |
| MA20 未接入 | ✗（誤判） | **NO_DATA** | —站上MA20 |

`passes_filters` 回傳新增 `no_data` 清單、`states` 三態字典。
`all_pass` 收緊為「**無 NO_DATA 且全 PASS**」——算不出的絕不算通過（對齊系統鐵律）。

### 3. 極端價防護（跌停/漲停 aflow 失真）
跌停時買方掛單被動成交被記成外盤（主動買），aflow 嚴重灌水失真。
`|漲跌%| >= EXTREME_PCT(9.0)` → 主動差 / 吸籌強度 / 象限全判 **NO_DATA**，標「極端價·訊號不可信」，不進符合清單。
`rank_potential` 中極端價股一律墊底。

範例（世界 5347：−9.72%、aflow +164359）：舊版會誤判強惜售全過；
新版三條全 NO_DATA、`all_pass=False`、`extreme=True`。

前端：`filter_display` 已含 ✓/✗/— 三態；另有 `filter_no_data`、`extreme_price` 可用。

---

## v4.1 更新：MA20 接線 + 每檔 AI 白話解讀

### MA20 接線（讓「—站上MA20」活過來）
`test_page_wiring.py` 把 MA20 快取接進測試頁：
```python
ma20_cache = prefetch_ma20.build_ma20_cache(api, UNIVERSE)          # 盤前 08:30 一次
prefetch_of = make_prefetch_of(ma20_cache, yesterday_vol, atr_map, trigger_map)
rows = build_rows(UNIVERSE, meta_of, prefetch_of, regime)           # 每輪 tick
```
接上後該欄「—」變真實 ✓/✗；未接入或資料不足個股仍顯示「—」（NO_DATA，不補造）。

### 每檔一行 AI 白話解讀（`ai_explain.py`）
兩層設計，永不開天窗：
- **local_explain**：純規則生一句話，免費即時必有輸出。結合象限/aflow/三態/極端價，說人話：
  - 跌停股 → 「逼近跌停，aflow 多是掛單被動成交非真吸籌，訊號不可信，先別碰」
  - 強惜售 → 「跌 X% 卻有大單承接（吸籌 Y%），跌深反彈候選，但屬抄底要配緊停損」
  - MA20 未接入 → 附註「引擎軌進出場暫無法驗證」
- **claude_explain**：可選，走 Claude API 潤飾；無 key 或失敗自動退回 local（對齊「API key 空白要能降級」教訓）。

前端每列多印 `row["ai"]` 即可。

---

## v4.2 更新：標的分類（三大群 + 象限細分）

`classify.py`：把相同情況的標的自動歸群，主軸「可操作性」。

| 群 | 定義 | 子分類 |
|---|---|---|
| **可操作** | 非極端價 + 象限對 + 條件過 | 真攻擊追漲（引擎軌，需含 MA20）／強惜售抄底（攻擊軌，不綁 MA20） |
| **觀察** | 有亮點未齊，或引擎軌 MA20 未接入待確認 | 條件待確認 |
| **排除** | 極端價失真、休息、假紅 | 極端價失真／假紅衝高／休息略過 |

關鍵設計（守華邦電教訓「軌道不混」）：
- **真攻擊追漲屬引擎軌**：需全過含站上 MA20；MA20 未接入 → 落到觀察。
- **強惜售抄底屬攻擊軌**：用觸發/ATR 進出，**不綁 MA20**；只需非極端價+強惜售+吸籌強度足即可操作。

用法：
```python
from app.classify import classify_all
result = classify_all(snaps, regime=F.market_regime(thermo["score"]))
# result["可操作"]["強惜售抄底"] → [排序後的候選...]
# result["counts"] → {"可操作":n, "觀察":n, "排除":n}
```
前端依三群分區塊渲染，群內子分象限；可操作群內依 aflow 強者在前。
你截圖那種滿頁惜售會自動切成「強惜售抄底(可操作)」與「跌停失真(排除)」兩堆，一眼分辨。

---

## v4.3 更新：分類排序 + 收盤蓋章盤後歷史

### 分類攤平排序（`classify_flat`）
解決「三群交錯、畫面很亂」：攤平成排序好的單一清單，前端照順序印即整齊。
- 群組順序：**可操作 → 觀察 → 排除**（能進場的最前）
- 群內：aflow 強者在前
- 每 row 標 group/subgroup，UI 可在群首插分隔標題

```python
from app.classify import classify_flat
rows = classify_flat(snaps, regime=F.market_regime(thermo["score"]))
```

### 收盤蓋章（`eod_stamp.py`）
13:30 把當日盤中最終結果存一份盤後歷史，與盤中即時表硬分離（守鐵律）。
- 獨立表 `intraday_eod`，**不碰 stock_state、不碰主站 STATE**
- `data_stage='eod_stamped'` 標記收盤定案，與盤中 `intraday_est` 區分
- 主鍵 `(trade_date, code)`：不同日各自獨立、同日重跑更新，不覆蓋歷史
- 極端價（跌停鎖死）照實存但標 `signal_reliable=0`，歷史完整不丟資料
- 存：收盤 aflow / 象限 / 分類群+子群 / all_pass / 極端價 / 盤勢

```python
from app.eod_stamp import run_eod_stamp, load_eod
run_eod_stamp("mls_intraday.db", snaps, thermometer_score, "2026-07-20")  # 13:30 觸發
load_eod("mls_intraday.db", "2026-07-20", group_name="可操作")            # 回溯
```

**重要澄清**：MA20 與盤後儲存無關——MA20 讀日 K 歷史（kbars 盤前算），
不依賴任何盤中儲存，就算盤中一筆沒存也照樣算得出。兩者獨立。

---

## v4.4 更新：盤中歷史查詢（單日 + 跨日追蹤）

### 後端查詢（`eod_stamp.py` 新增）
- `load_eod(db, date, group_name)` — 單日回看：某天全部股票分類，可按群過濾。
- `load_stock_history(db, code, days)` — 跨日追蹤：某檔近 N 日分類/aflow 變化（由舊到新）。
- `list_trade_dates(db)` — 有資料的交易日（日期選擇器用）。
- `stock_trend_summary(db, code, days)` — 跨日訊號濃縮：持續可操作／分類爬升／惡化／反覆。

### 前端（`history_page_demo.html`，獨立分頁 `/intraday-history`）
- **單日為主**：頂部日期選擇器 → 那天收盤分類，沿用可操作→觀察→排除排版。
- **點個股展開跨日**：近 N 日分類軌跡條 + aflow 走勢。
  - 持續可操作 = 強訊號；分類爬升（排除→觀察→可操作）值得留意；分類反覆 = 訊號不穩。
- 極端價當日標「訊號不可信」，照實存但吸籌欄顯示「—」。

**為什麼兩種都做**：單日是基本回看，但跨日才是金礦——同一檔連續留在可操作、
或從排除爬升，是單日快照發現不了的訊號。富鼎範例：aflow 由流出翻流入、逐日增強 → 分類爬升。

---

## v4.5 更新：TWSE 官方資料接入 + 明日觀察清單

### TWSE 抓取（`twse_fetch.py`）—— 解你「一直抓不到」的雷
用新版 keyless OpenAPI gateway `openapi.twse.com.tw/v1/`（比舊 CSV endpoint 穩）。
你以前抓不到的常見原因，已在程式解掉：
1. **User-Agent 被擋** → 一律帶瀏覽器 UA（舊 CSV endpoint 無 UA 會回 HTML/空）。
2. **endpoint 用錯** → 用新 OpenAPI JSON gateway，不用舊 www.twse.com.tw/fund/T86 CSV。
3. **日期格式** → gateway 為當日全市場快照，不需帶日期。
4. **收盤前抓=空** → 法人約 15:00 後出，太早抓是空的（非程式錯）。

抓 `fetch_institutional()` 三大法人、`fetch_margin()` 融資融券；任何失敗回空 dict，
呼叫端標 NO_DATA、不補造、不崩潰。連買天數 `accumulate_streak()` 自行累計歷史。

### 明日觀察清單（`tomorrow_watchlist.py`）
核心：盤中資料負責「今天能不能追」，盤後資料負責「明天值不值得等」。
- 只納今日「可操作/觀察」（排除群不進）。
- 疊加 TWSE 法人/融資 → 判四狀態 Ready / Watch / Pullback Watch / Pass。
- **TWSE 抓不到 → 最多 Watch，不給 Ready**（無籌碼驗證不進場），標「缺籌碼」不崩潰。
- 券商分點/主力方向：資料源未接 → NO_DATA，標「加分項·未接入」小字，不影響主流程。

四狀態判定：
| 狀態 | 條件 | 明日 |
|---|---|---|
| Ready | 可操作+法人買超+融資健康 | 可進場候選 |
| Pullback Watch | 真攻擊+法人買超但追價風險高 | 等回踩均價/MA5 |
| Watch | 價格強但籌碼分歧/未接入 | 等突破或回測 |
| Pass | 法人賣超 / 融資暴增 | 移出清單 |

VPS 用法（APScheduler 15:30 收盤後）：
```python
from app.twse_fetch import fetch_institutional, fetch_margin
from app.tomorrow_watchlist import build_watchlist
inst = fetch_institutional(UNIVERSE)     # 抓不到回 {}，自動降級
mg = fetch_margin(UNIVERSE)
wl = build_watchlist("mls_intraday.db", "2026-07-20", inst, mg, streak_prev)
```

**準度備註（小字）**：法人為當日淨額；融資以 TWSE 當日餘額差計；
接了券商分點會更能分辨換手/倒貨（尚未接入，不影響現有判斷）。
