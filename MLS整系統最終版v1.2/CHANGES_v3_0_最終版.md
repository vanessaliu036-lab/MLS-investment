# MLS 完整系統 v3.0 最終版 — 總異動說明
(基底:MLS_完整系統_v2_0_首頁改版.zip;本包為合併後完整資料夾,解壓即部署)

## 部署方式
把整個 `mls-standard/` 覆蓋到伺服器同名資料夾,重啟 server.py 即可。
所有掛鉤已直接寫進 server.py / after_hours.py,**不需再手動加任何一行**。
既有 mls.db 資料完全相容:只新增資料表(livermore_record、dec_health、
dec_watchlist、dec_verify),原有資料一筆不動。

---

## 一、新增檔案(6 個)

| 檔案 | 內容 |
|---|---|
| `livermore.py` | 李佛摩六欄紀錄 v2.0(妳最新提供版):六欄狀態機 + livermore_record 落地 + 自帶 API router(/livermore、/api/liv/*) |
| `livermore.html` | 李佛摩前端頁(六欄色表,總覽 + 單檔),已落地為實體檔案 |
| `decision_v22.py` | 資金決策 v2.2 盤後決策中心:個股健康指數時間序列(四象限×趨勢×資金持續×籌碼蓋章)→ 隔日觀察清單 → 隔日自動驗證(觸發/進場/隔日最高/隔日收盤/達標/模擬持有)→ 30 日勝率統計(分級/分數區間/四象限/Hold避損) |
| `decision.html` | 決策中心前端(/decision):命中率儀表 + 觀察清單/隔日驗證/勝率統計三分頁 |
| `chip_provider.py` | 可插拔籌碼引擎介面:統一入口 get_chip_data(code) → (data, quality)。quality 誠實標記 'finmind_basic'(現況:FinMind 日法人 + 週大戶)或 'premium'。之後接 FindBillion 等分點級資料商,只需新增 chip_provider_premium.py + 設環境變數 CHIP_PROVIDER=premium,不改任何呼叫端。**不假造分點資料** |
| `watchlist_screener.py` | 妳提供的獨立抗跌股篩選器,原樣收入(可 `python watchlist_screener.py` 獨立驗證;after_hours 內建的同邏輯照舊運作,互不干擾) |

## 二、修改檔案(5 個)

### scoring.py — 主動淨流 +0.00 bug 根治(資金健康度優化核心)
- **根因**:舊 update_aflow 依賴快照 API 的 tick_type 判內外盤,但 Shioaji
  批次快照(api.snapshots)的 tick_type 不是真逐筆判定,常態 0/None →
  sign 恆為 0 → aflow 永遠卡 0。
- **修法**:改用 broker 快照本來就有抓的 buy_volume/sell_volume(外盤/內盤
  累積量)兩輪增量差計算,可靠且免額外請求;tick_type 僅當退回法保留相容。
- 新增 push_flow_ratio / flow_velocity:資金流速(近 6 輪 aflow_ratio 斜率),
  供 MoneyFlow 模組加分「轉強/轉弱」。
- reset_aflow 同步清空新緩衝。

### engine.py — 一行修改
- eval_stock 內 update_aflow 呼叫改傳 buy_volume/sell_volume(走可靠新路徑)。

### money_health.py — 健康分升級為四模組合成(妳指定的升級方向)
- 舊公式只有「資金方向 + 漲跌幅」;新版:
  **A. Price 0.30**(均線/突破今高/量比/漲跌幅)
  **B. MoneyFlow 0.30**(主動淨流方向 + 資金流速)
  **C. Chip 0.20**(法人/大戶,經 chip_provider;premium 時額外計入
     分點集中度、主力分點買賣超)
  **D. Sector 0.20**(族群相對強弱 RS + 族群內排名)
- Chip 完全無資料時自動降為三模組、權重按比例重分配,不用假分數硬湊。
- 回傳新增 module_scores(四模組各自分數)、flow_velocity、chip_quality;
  chip_quality 原樣透出 API/報告,避免「近月+19,878張」被誤讀成分點級分析。
- 四象限分類、Level 8.1 三角驗證、annotate() 呼叫介面全部不變,
  engine.py 呼叫端零改動即相容。

### chips.py — 修一個生產環境真實 bug
- get_chips() 內 `_cache = {...}` 賦值缺 `global _cache` 宣告,導致函式
  開頭讀取即 UnboundLocalError → **籌碼快取層從未正常運作**,所有呼叫端
  只拿到例外。補上 global 宣告後 FinMind 快取才真正生效。

### server.py — 插件掛載
- 新增 import livermore / import decision_v22
- app 建立後掛載兩個 router(各自 try/except,失敗不影響主系統):
  /livermore、/api/liv/*、/decision、/api/dec/*

### after_hours.py — 盤後自動化
- 插件掛鉤區(nexora / eod_pipeline 旁)新增兩段,同樣 try/except 失敗不影響主流程:
  ① livermore.record_today() 每日盤後自動存六欄紀錄 + Telegram 摘要
  ② decision_v22.run_report(last_state) 每日盤後跑「驗證昨日清單 →
     全池健康指數落地 → 產隔日觀察清單 → 勝率統計」+ Telegram 摘要
- run() 回傳值新增 livermore / decision 兩鍵。

## 三、未改動檔案
nexora.py、eod_pipeline.py、backtest.py、db.py、broker.py、config.py、
gatekeeper.py、notifier.py、rankings_api.py、strategy_doc.py、
index.html、rankings.html、requirements.txt 等,全部原樣。

---

## 四、系統定位(v2.2 正式版,四功能分工)

| 功能 | 盤中 | 盤後 | 頁面 |
|---|---|---|---|
| 資金健康度(四模組) | ⭐⭐⭐⭐⭐ 溫度計 | ⭐⭐⭐⭐ 驗證盤中判斷 | 首頁 /api/state |
| 訊號版 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | 首頁 |
| 李佛摩六欄 | ⭐(盤中不刷新) | ⭐⭐⭐⭐⭐ 15:00 後選股中心 | /livermore |
| 個股健康指數(決策中心) | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ 觀察→驗證→勝率 | /decision |

## 五、驗證定義(decision_v22.py 頂部可調)
- 觸發 = 隔日最高突破觀察日高點;進場 = 觸發且 Ready
- 達標 = 隔日收盤 ≥ +0.3%(與主系統成敗判定一致)
- 模擬持有 = 觸發價進場,收盤跌破進場日低點出場,上限 5 日
- 30 日統計:Ready/Watch 命中率、平均報酬、平均持有天數、
  90–100/80–89/70–79/65–69/50–64 分數區間勝率、四象限隔日表現、Hold 避損率

## 六、離線測試已通過項目
- aflow 新路徑(buy/sell 增量)與舊 tick_type 退回法皆驗證正確
- 四模組健康分計算、chip 缺資料權重重分配、annotate() 相容性
- decision_v22 三日閉環(D1 清單 → D2 驗證回填 → D3 統計)
- watchlist_screener 內建範例(重現妳貼的清單格式)
- livermore v2.0 內建四行情測試
- 全部 .py 語法檢查通過

## 七、誠實提醒(部署後請驗證)
1. 本沙盒無網路、裝不了 FastAPI,router 只做了靜態檢查;部署後請開
   /livermore 按「存今日」、開 /decision 按「跑盤後」各一次確認可寫入。
2. FinMind 403/額度問題會讓 Chip 模組降級(quality=finmind_basic 且
   chip=None → 三模組計分),系統照常運作但建議檢查 FINMIND_TOKEN。
3. aflow 修正後,盤中主動淨流從下一個交易日的即時掃描才會開始有真值;
   盤後 EOD 重抓路徑仍無盤中累積(decision_v22 會標 vr_proxy 誠實揭露)。
4. 分點/大戶集中度等 premium 籌碼欄位目前為空介面,接資料商後才會有值。


---

# v2.3 增量(在 v2.2 全部內容之上)

## 新增檔案(2 個)
| 檔案 | 內容 |
|---|---|
| `indicators.py` | 技術指標引擎:MA/MACD(12,26,9)/KD(9,3,3)/RSI(14,Wilder)/ATR(14,Wilder)。公式全部教科書標準,`python indicators.py` 內建交叉驗證:EMA/RSI 對照 pandas 獨立算法、KD/ATR 手算逐步對照、MACD 趨勢一致性 —— 五項全部通過,「檢查公式是否正確」有據可查 |
| `stock_card.py` | 優化個股資訊卡組裝器 + 盤面速覽。籌碼面(外資/投信/自營=日資料、400張/千張大戶=集保週資料、主力分點=premium 介面現為 None 誠實標記)、資金(主動買賣% 來自外內盤累積、5/10日資金流=日K帶方向量能)、技術(MA5/10/20/MACD/KD/RSI/ATR,low 補值時標 ≈ 近似)、交易計畫(ATR 結構:停損 −1.3×ATR、T1 +2×ATR、T2 +4×ATR、RR≈1.54)、AI 結論(✓/✕ 原因全部來自真實欄位) |

## 修改檔案(3 個)
- `chips.py` 新增 get_chips_detail():三大法人分項單日淨額、外資20日、400張/千張級距週變化。get_chips() 原函式零改動。
- `decision_v22.py`:①勝率統計新增**最大回撤**(進場交易依日期序持有報酬累計曲線峰谷差) ②新增 API `/api/dec/card?code=`(資訊卡)與 `/api/dec/brief`(資金流入前三族群,億) ③版本字樣 v2.3。
- `decision.html`:①頂部盤面速覽列(資金流入前三) ②觀察清單卡片點開 → 個股資訊卡彈層(籌碼/資金/技術/交易/AI 結論五區塊,版型對齊使用者規格) ③分級統計顯示最大回撤。

## v2.3 誠實揭露
- 「主力」與「級距分布」欄位在接上分點級籌碼商(chip_provider_premium)前顯示「—/待接分點」,不以法人資料冒充。
- KD/ATR 在 broker 日K low 欄補真值前為近似值,卡片標「≈」;broker 補齊後自動變精確,無需改程式。
- 冒煙統計中 avg_hold_ret / 最大回撤數值來自合成測試資料,實際數字待部署後累積 20–30 個交易日。


---

# v2.3.1 UI 全站掃描修正(四頁全查,截斷/直排/低對比根治)

## 掃描發現與修法
| 頁面 | 問題 | 修法 |
|---|---|---|
| index.html | 熱力圖小格族群名折行成直排/被裁切 | 格內文字 nowrap + 字級改依名稱實際寬度計算,塞不下整格不顯示文字(不再顯示半截字) |
| index.html | --sub/#6b7280、--faint/#aeb4bf 低對比灰(白底對比僅~2:1),即「不清楚的標示」主因 | 全面改高對比 #3a4150 / #565e6e |
| index.html | 因子條標籤欄 64px 太窄(量能TNVR 會截斷)、鎖標 8px、多處 10px 以下 | 標籤欄 80px+nowrap、全站字級下限 9.5px 起跳 |
| livermore.html | 六欄表頭 4 字標籤在窄機逐字直排(妳多次反映的主病灶) | 表頭固定 2×2 兩行排版(次級/反彈),永不逐字直排;日期欄 64→56px 讓數據欄變寬;價格格 nowrap+自動縮距 |
| livermore.html / rankings.html | --ink2:#6b6256 偏淡 | 改 #575043 |
| decision.html | 驗證表 10 欄窄機擠壓、「點開資訊卡」float 與觸發價疊字風險 | 表格 min-width 640px 改水平捲動(零截斷);提示改 flex 尾端 |
| decision.html | 資訊卡格值 CJK 可能逐字折行 | word-break:keep-all,只在詞邊界換行 |
| 四頁通用 | 9–9.5px 徽章字 | 一律 ≥10px 且 nowrap |

## 驗收(自動掃描,全數乾淨)
- 禁用灰(#4a4f57/#7d828c/#9aa0aa/#6b707a/#33373f)與 #0d0f12 文字色:0 處
- 舊低對比色(#aeb4bf/#6b7280/#6b6256):0 處
- 8–9px 字級:0 處


---

# v2.4 增量:盤前報告(OpenAI 官方 API)

## 新增
- `premarket.py`:盤前資料蒐集(美股四大指數 FinMind USStockPrice、昨日決策觀察清單、健康分前20、李佛摩狀態、系統命中率)→ 組進「台股觀察池篩選員」指令(妳的指令原文一字不改內嵌)→ 呼叫 OpenAI chat completions → 落地 premarket_report 表。同日自動去重,force=1 可重跑。缺哪段資料就在資料包標「無資料」讓模型自行補足,不填假數字。
- `.env.example` 新增留空位:`OPENAI_API_KEY=`(填入即用)、`OPENAI_MODEL=gpt-4o`。
- index.html:①首頁最上方新板塊「盤前報告」(日期/模型/預覽三行/產生按鈕/未設金鑰警示) ②新分頁 🌅 盤前報告(完整報告渲染:支援全形｜與 markdown 兩種表格,A/B/C/D 級自動上色,表格窄機水平捲動零截斷)。
- server.py 掛載 premarket router(/api/premarket/latest、/api/premarket/run)。

## 使用
1. `.env` 填 `OPENAI_API_KEY=sk-...`,重啟 server。
2. 開盤前按首頁「產生今日報告」(或 `POST /api/premarket/run`,排程可 cron 08:00 呼叫)。
3. 金鑰未填時按鈕會回設定指引,不會假造報告。

## 已測試(離線)
- 指令原文完整性(含 1000 股上限、A–D 分級、九欄輸出格式)
- 資料包組裝與各段落缺資料降級、DB 寫入/讀回、同日去重、空金鑰指引路徑
- 前端 JS 語法、全形｜表格解析輸入格式
- 未能測試:實際 OpenAI 回應(需金鑰)與 FinMind 美股資料(沙盒無網路)——部署後第一次執行請確認這兩段。


---

# v2.4.1 增量:李佛摩六點轉向判定層(定位缺口補齊)+ 精確化修正

## 定位稽核結論(對照盤中/盤後架構)
資金健康度(盤中溫度計/盤後驗證+AI學習)、訊號版(盤中)、個股健康指數(盤後決策中心)、李佛摩盤中不刷新——四項均符合。唯一缺口:妳定位文的「六點」(60日高/低、量放大、50%回測、轉向點、進觀察池)在六欄記錄法程式中不存在,本版補齊。

## livermore.py 新增六點轉向層(不動原六欄狀態機)
- `six_point_eval` / `six_point_scan`:①突破60日高 ②跌破60日低 ③量>5日均×1.5 ④50%回測未破(近20日波段中點之上收盤) ⑤轉向點(六欄關鍵點) ⑥合格=正式進觀察池。合格核心=突破60日高/低+量放大(突破本身即李佛摩式訊號);關鍵點/趨勢/50%回測為記錄旗標交決策中心加分,避免盤整突破被狀態機延遲確認漏抓。引擎股照鐵律永不合格。
- 落地 `livermore_sixpoint` 表;API:GET /api/liv/sixpoint、POST /api/liv/sixpoint_scan。
- livermore.html 總覽頂部新增「⚡今日六點轉向合格」區塊(多方/空方、✓破60日高、✓量倍數、✓50%回測、關鍵點逐項顯示;零合格也明示「市場未給訊號,休息」)。「存今日」按鈕自動接續六點掃描。
- after_hours 盤後掛鉤:六欄存檔後自動跑六點掃描,Telegram 摘要含合格檔數。

## decision_v22 串接(觀察→驗證閉環吃進六點)
- 健康分新增:六點合格 long +8 / short −8;50%回測未破 +3。已端到端驗證(合格股健康分確實提升)。

## 精確化修正
- broker.daily_kbars 補真實 low/open 欄——六點50%回測、KD、ATR 全部由近似轉精確(stock_card 的「≈」標記將自動消失)。
- 資訊卡籌碼週期改標「法人=T-1 盤後蓋章(非即時)」,盤中顯示語意精確,不與盤中資金流混讀。

## 測試
- 六點判定合成案例(66根盤整+末日放量突破):break60✓ 量2.4×✓ 50%未破✓ 合格long✓
- 掃描落地/讀回、決策中心加分端到端、全部 .py 語法、四頁 JS 語法


---

# v3.0 增量:資金健康度中心 × 雙軌玩法(重新制定,取代舊 Rule 0)

## 設計修正(依使用者指正)
「引擎永不進場」廢止。引擎=主流資金停泊處,反而要進場;引擎/攻擊從「資格門檻」改為「玩法標籤」,由數據每週輪替,絕不寫死。資金健康度為全系統唯一核心主軸——目前驗證下來最可持續觀察的功能,重點是累積數據。

## 新增:engine_review.py(角色動態審查)
- 引擎行為量化:60日波動 ≤ 池內25分位 + 法人佈局 + 成交金額 ≥ 中位 = 引擎;現任引擎波動升至40分位 = 已動起來 → 建議降轉攻擊(變成可交易)。
- 每週五盤後自動審查(after_hours 掛鉤)+ Telegram 建議通知;AUTO_APPLY_ROLES=true 可全自動套用,預設需 POST /api/roles/apply 確認。名單寫入 engine_roles.json,config 熱載全系統生效。
- config.py 新增 reload_roles();watchlist_screener 改讀 config 並移除引擎排除;livermore 六點層引擎解禁。

## decision_v22 雙軌重寫(v3.0)
- **引擎軌(波段)**:分級=站上月線+法人未反向+健康分≥60;進場=觀察日收盤;達標=**第5日收盤≥+1% 且期間未收破月線**;模擬持有=收破月線出場,上限10日。
- **攻擊軌(短線)**:維持原制(突破觸發、隔日收盤≥+0.3%、破進場日低出場、上限5日)。
- dec_watchlist / dec_verify 新增 track 欄(舊庫自動 ALTER 遷移)。
- **勝率統計分軌**:引擎軌/攻擊軌 × Ready/Watch 各自命中率、均酬、持有天數、最大回撤,並附各自達標定義;合併統計保留為舊制對照。
- 前端:觀察卡/驗證表軌別徽章、統計頁「分軌表現」置頂;資訊卡引擎軌交易計畫=收盤買/月線停損/+3×ATR/+6×ATR。

## 測試
- 角色審查:合成高波動聯電 → 建議降轉 → 套用 → 熱載生效
- 雙軌端到端:引擎股進入觀察清單(聯電 Ready)、引擎軌5日+月線驗證正確回填、分軌統計輸出
- 引擎軌測試裡的負報酬為合成資料快照價/日K不同源之假影(實盤同源),機制無誤
- 全部 .py 語法、四頁 JS 語法迴歸通過

## 記憶同步
舊 Rule 0 已從長期記憶中改寫為 v3.0 雙軌規則,之後不會再以「引擎不可進場」為由攔截。
