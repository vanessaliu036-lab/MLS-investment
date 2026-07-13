# 李佛摩價格紀錄插件 — 安裝說明

## 這是什麼
李佛摩原書「六欄手寫價格紀錄簿」的數位版。每天把固定觀察池每檔股票的價格,
依其走勢落進六欄之一(次級反彈/自然反彈/上升趨勢/下降趨勢/自然回檔/次級回檔),
存進 mls.db 累積成歷史;並偵測「關鍵點(Pivotal Point)」——突破前一趨勢極值的轉向訊號。

## 完全獨立,不改你現有任何檔案
本插件**只新增兩個檔**,不動 index.html / db.py / after_hours.py / engine.py 等。
你部署那版的所有既有分頁、功能一律照舊。

## 要放進資料夾的檔案(共 2 個)
1. `livermore.py`    ← 後端(引擎 + mls.db 存檔 + 自帶 API router)
2. `livermore.html`  ← 前端(六欄色表頁面)

放進 mls-standard/ 資料夾即可(跟 server.py 同層)。

## 啟用:server.py 只加「一行」
在 server.py 任意位置(建議 `app = FastAPI(...)` 之後)加:

```python
import livermore
app.include_router(livermore.router)
```

就這樣。這一行不會影響任何既有路由。

## 開啟頁面
部署後瀏覽器開:  `http://你的位址/livermore`
（例如 http://104.156.239.83/livermore）

若要嵌進你現有分頁列:在你的分頁「李佛摩」連結指向 `/livermore` 即可。

## 每日自動存檔(選用)
本插件預設可「手動存檔」——頁面右上「存今日」按鈕會立刻抓盤後價寫一列。
若要每天盤後自動存,在你 server.py 盤後排程(或 after_hours.run 結尾)加一行:

```python
import livermore
livermore.record_today()      # 對觀察池 50 檔各存今日一列
```

不加也沒關係,用「存今日」按鈕手動觸發即可。

## 資料存哪
- 存進你現有的 `mls.db`,新表名 `livermore_record`(CREATE TABLE IF NOT EXISTS)。
- **你原本的資料一筆都不會動**,只是多一張表。
- 欄位:trade_date, code, name, sector, stock_type, price, high, low, state, pivot, pivot_price。

## API(前端自動呼叫,無需手動)
- `GET  /livermore`              頁面
- `GET  /api/liv/overview`       觀察池最新六欄狀態總覽
- `GET  /api/liv/record?code=XX` 單檔六欄歷史(即時由日K重算完整序列)
- `POST /api/liv/snapshot`       手動抓價存今日
- `GET  /api/liv/dates`          已存檔日期清單

## 鐵律已內建
- 主引擎股(2303 聯電、5347 世界)在總覽標「溫度計」,不當進場標的。
- 關鍵點僅由「價格結構」判定;法人買賣一律另由 chips 盤後確認(與 NEXORA Hard Rule 一致)。

## 可調參數(livermore.py 頂部)
- `PIVOT_SWING_PCT = 6.0`  主狀態切換門檻(李佛摩約六點擺動)
- `SEC_SWING_PCT = 3.0`    次級波動門檻
- `KBAR_DAYS = 90`         詳細表回看日數

## 資料來源
與主系統相同:永豐 Shioaji(透過現有 `broker.daily_kbars`)。金鑰仍走環境變數,本插件零金鑰。
broker 日K目前僅回 (date, close, high, volume),low 以 close 保守補值,
不影響上升系關鍵點主邏輯;日後 broker 補 low 欄位會自動吃進,無需改本插件。
