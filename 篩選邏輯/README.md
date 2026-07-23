# MLS v4.0 — 七條規範與實作對照

## 部署
```
pip install fastapi uvicorn
python preflight.py          # 先跑自檢,不過就不要啟動
uvicorn api:app --host 0.0.0.0 --port 8000
```

## 七條規範 → 哪支檔案擋住

| # | 規範 | 實作 |
|---|---|---|
| 1 | 時段隔離,盤中不打 FinMind | `phase.py` |
| 2 | 同一天只抓一次,寫進 DB 永不重抓 | `store.fetch_once()` |
| 3 | 不做交叉驗證,誰先回誰算數 | `store.fetch_once()` sources fallback |
| 4 | 盤中一套、盤後一套,各出一份名單,前端不准 filter | `screen_intraday.py` / `screen_post.py` / `index.html` |
| 5 | 一表一 owner,插件包信封,壞掉不互相影響 | `store.TABLE_OWNER` / `envelope.py` |
| 6 | 每份名單標明用途 | `phase.describe()` → `purpose` 欄位 |
| 7 | 每個插件有自己的取數通道 | `envelope.run_all()` 各自獨立讀取 |

## 三個時段

| | 資料來源 | 回答 | 動作 |
|---|---|---|---|
| PRE 00:00–08:59 | 直接讀昨日盤後名單,零 API | 今天盯誰 | 只觀察 |
| INTRADAY 09:00–13:30 | Shioaji 訂閱 + DB 昨日死值 | 現在誰在被吸籌 | 只記錄,不下單 |
| POST 13:31–23:59 | 今日法人/融資 + 今日盤中累積 | 明天進誰 | 依此執行 |

盤前不重算 —— 盤前就是昨天盤後的結果。

## 四層防護

1. **寫入權限鎖** — 一張表一個 owner,非 owner 寫入直接 raise
2. **歷史不可變** — SQLite trigger 層面擋,繞過 Python 也擋得住
3. **信封隔離** — 插件壞掉只影響自己那格,名單照出
4. **啟動自檢** — 六項檢查,任一不過服務不啟動

## 驗收條件(已通過)

- 清空 FinMind API key → 盤中名單照出,API 呼叫 0 次
- `screen_post.py` 整支 rename 掉 → 盤中照跑
- 新插件寫 `inst_flow` → 被擋,已驗證盤後值不變
- 重開服務 → API 新增呼叫 0 次
- 任兩個分頁前 10 檔代號與順序完全一致
- 注入前端 `.filter()` → preflight 擋下

## API 用量

| 情境 | 呼叫次數 |
|---|---|
| 盤前開機 | 0 |
| 盤中整場 | 0 |
| 盤後跑一次 | 51 |
| 重開服務 | 0 |

一天 51 次。TWSE/TPEx 官方免費無上限,FinMind 僅作備援。

## 新增插件的規矩

1. 建自己的新表,呼叫 `store.register_table("my_table", "my_plugin")`
2. 讀別人的表隨便讀,寫別人的表一律被擋
3. 插件函式透過 `envelope.run_plugin()` 呼叫,爆掉不會傳染
4. 取數一律走 `store.fetch_once()`,不准自己直接打 API
