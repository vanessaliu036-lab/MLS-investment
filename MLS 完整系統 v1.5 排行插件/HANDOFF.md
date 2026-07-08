# MLS 標準版 — 完成版說明(v1.0 FINAL)

> 原「待對接清單」六項已全部完成並通過整合測試,**無需再交接給下一個 AI**。
> 即時數據唯一來源 = 永豐 Shioaji。金鑰全部走環境變數,程式碼零金鑰。

## 決策流程(使用者定案)
```
① 資金流入板塊計算  flow_score = 金額佔比增幅×0.6 + 中位漲幅×0.4
② 取前三族群
③ 每族群 1 檔龍頭   金額×0.5 + 漲幅×0.3 + 量比×0.2(族群內排名)
④ 龍頭深度分析      法人近月買賣超 / 外資連買天數 / 千張大戶%與趨勢
                    / MA20乖離(均線上方空間) / 距60日前高 / AI建議
⑤ 其餘候選 → 規則引擎 → 熱力表(進場/觀察/監控/出場)
```

## 檔案結構(10 檔,全部完成)
```
config.py      參數 + 族群表(使用者定案 10 群組 50 檔固定池,11 族群,含主引擎/攻擊部隊分類)
broker.py      Shioaji:登入(20h自動重登)/scanner三榜/snapshots/指數/日K
chips.py       籌碼:FinMind 盤後日更(法人/大戶),當日快取
engine.py      ①~⑤ 決策流程 + W1~W4 篩選 + 現金閘門掛載
gatekeeper.py  現金閘門:positions.json 持股/次數上限,滿手降級進場訊號
db.py          SQLite:signals/sector_snapshot/watchlist/review_log
notifier.py    Telegram 分級推播,冷卻 10/30/5 分鐘;無 token 自動 dry-run
after_hours.py 盤後複查:收盤驗證命中率/遺漏、抗跌股→明日觀察清單、
               Airtable 同步(無 token 自動跳過)、開盤重驗(08:55)
server.py      排程總管:08:30載清單→08:55重驗→盤中迴圈→15:05盤後
index.html     前端:①板塊排行 ②龍頭深度分析(三欄) ③熱力表 + 閘門橫幅
```



## v1.2 準度工程(學習為核心)
**評分公式(scoring.py)**
```
Score = clamp( Σ 因子×動態權重 − 懲罰 ) × 環境係數(攻擊1.0/謹慎0.85/風險0.6)
因子:趨勢25 量能25(TNVR時段正規化) 相對強度20 籌碼20 族群10
懲罰:假紅背離−15 邊拉邊賣−12 乖離>8%−10 爆量滯漲−8
TNVR = 累積量 ÷ (5日均量 × 全日量U型曲線f(t))
aflow = Σ Δ量×(外盤+1/內盤−1) → 價量背離=個股層級鐵律2/8
```
**學習迴圈(權重拉高)**:因子30日命中率→權重 clamp 0.5~2.0;
命中<45% 因子休眠(0.5)。盤後自動更新,開盤自動載入。
**80%準度控制器**:成功=收盤>訊號價+0.3%;近10日精度<80%→門檻+3(上限70),
>85%→門檻−2(下限35)。kv:entry_score_min。
**回撤斷路器(盤中)**:訊號跌破建議停損=失敗;當日連3敗→停發新進場
(記錄與學習照常;出場訊號永不受影響)。開盤重置。
**回測預訓練**:`python backtest.py 60` 日K回放→權重+門檻寫入 mls.db,
部署首日即帶參數。誠實聲明:日K近似盤中,校準相對權重而非逐筆重現;
80%為控制目標非保證,達不到時系統自動收緊而非硬湊。
**部署即用**:pip install -r requirements.txt → 填.env → python backtest.py
→ python server.py。無需改任何程式碼。

## 部署檢查清單(v1.1 補齊)
1. `pip install -r requirements.txt`(版本已鎖範圍;可 pip freeze 覆蓋為精確版)
2. `cp .env.example .env` → 填 SHIOAJI 兩把金鑰(其餘選填,留空=降級不影響運行)
3. `cp positions.json.example positions.json` → 填實際持股
   (未建檔首次啟動會自動生成空範本;**持股為空=現金閘門永不觸發**)
4. `python server.py` → http://127.0.0.1:8000
5. 公網:VPS 常駐(覆蓋 08:30–15:10)+ Cloudflare Tunnel/nginx。
   Shioaji 長連線,Vercel/serverless 不可用——與 FinMind Vercel 版是兩條獨立部署線。

## 執行(部署方式;實際執行使用者自理)
```bash
pip install shioaji fastapi uvicorn pandas python-dotenv
# .env(全部留空位,自行填入;程式碼不含任何金鑰):
#   SHIOAJI_API_KEY=
#   SHIOAJI_SECRET_KEY=
#   FINMIND_TOKEN=          選填(籌碼)
#   TELEGRAM_BOT_TOKEN=     選填(空=console dry-run)
#   TELEGRAM_CHAT_ID=       選填
#   AIRTABLE_TOKEN=         選填(空=僅存本地 SQLite)
#   AIRTABLE_BASE_ID=       選填
python server.py   # → http://127.0.0.1:8000
```
- 需常駐主機覆蓋 08:30–15:10;Shioaji 長連線不可上 serverless。
- 公網訪問:VPS + Cloudflare Tunnel / nginx 反代。
- 持股維護:編輯 positions.json(格式見 gatekeeper.py 註解)。

## API
- `GET /api/state`   盤中完整狀態(market/sectors/leaders/stocks/gate)
- `GET /api/review`  近30日命中率 + 今日統計 + 今日觀察清單

## 每日自動閉環
```
08:30 載入今日觀察清單(昨日盤後產出)
08:55 開盤重驗:跳空跌>2% 自動降級 + 推播
09:00–13:35 每30秒:掃描→龍頭分析→訊號diff→SQLite→冷卻推播
             族群新鎖定即推播;每5分鐘族群快照落地
15:05 收盤驗證(命中率/遺漏)→ 抗跌股篩選 → 明日觀察清單
      → Airtable 同步 → Telegram 摘要
```

## 鐵則(已內建,違反即 bug)
- 主引擎永不產生進場訊號;龍頭分析文案必含「不列入進場」。
- 出場/風險訊號不受現金閘門影響,永遠推播。
- 盤中資金流 ≠ 法人買賣;法人一律盤後資料確認。
- 訊號無論是否推播都寫入 SQLite(學習資料完整性)。

## 整合測試已驗證
清單載入→重驗→盤中兩輪(去重/冷卻)→龍頭與熱力表命中標記→
閘門滿手降級→盤後命中率66.7%計算→遺漏回推→明日清單→摘要推播→
四張資料表落地。Airtable/Telegram 未設 token 時優雅降級,系統照常運作。
