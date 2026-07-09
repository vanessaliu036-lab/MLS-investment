# 2026-07-09 收盤後 — 資金健康度 v3 Freeze 紀錄

**狀態**:🟢 已驗證、不可更動 — **凍版,禁止更動**  
**版本**:資金健康度 v3.0(money_health.py / server.py / index.html / health_history.py)  
**驗收日**:2026-07-09 22:25 by Vanessa  
**部署路徑**:`/Users/vanessaliu/Desktop/01_投資分析/MLS 完整系統 v1.2 /MLS整系統最終版v1.2/`

---

## ⚠️ 不可動原則(Hard Rule)

本檔描述的任何 v3 改動,**未經 Vanessa 明確口頭/文字核准前,不可更動**。
違反這條 = 直接退回上版 + 立刻回報 Vanessa。

**例外**(仍要問):
- 修到不能 compile / 完全 crash 主流程
- Vanessa 主動指示修改

---

## v3 改動範圍(已 commit + 已驗證)

| 檔案 | 內容 | 狀態 |
|---|---|---|
| `money_health.py` | 加 `chip_score()`(法人買賣超→0-25 分)+ `recompute_health_score()`(v3 公式) | ✅ 凍版 |
| `server.py` | `/api/money_health` 改成讀 `config.UNIVERSE` 50 檔全觀察池(不走 `engine.build_state()` 切片) | ✅ 凍版 |
| `health_history.py` | 新檔:每日 snapshot 存檔 + 命中率統計 + 時間序列 API | ✅ 凍版 |
| `index.html` | 加 `renderHealth()` 函數 — 「資金健康度」獨立 tab 頁 | ✅ 凍版 |

---

## v3 規格(已實作並驗收)

### 1. 健康分 v3 公式
```
健康分 = 資金流分(0-50) + 價量分(0-20) + 族群分(0-5) + Chip Score(0-25) = 100
```

### 2. Chip Score 規則
- 大買 ≥+5000 張近 20 日 → 25 分
- 小買 +500~+5000 → 15 分
- 中性 ±500 → 10 分
- 小賣 -5000~-500 → 5 分
- 大賣 ≤-5000 → 0 分
- 外資連買≥3 日 +3、≥5 日 +5
- 外資連賣≥3 日 -3、≥5 日 -5

### 3. 列表報告模式
- 「資金健康度」獨立 tab 頁(頂部 tabs 第 5 個)
- 不再依賴「點擊個股 modal」
- 50 檔全觀察池呈現 + 4 象限圖 + 命中率驗證區塊

### 4. 每日存檔
- 路徑:`reports/health_score_history/YYYYMMDD.json`
- 包含每張卡的 5 欄 metrics + Chip 評語 + 觸發/失效 + 進場/停損/目標

### 5. 命中率統計
- 健康分 ≥65 / 50-64 / <50 三組的隔日報酬率
- in_up / in_down / out_up / out_down 四象限的隔日報酬率
- 30 天後驗證模型預測力

---

## 7/9 驗收快照

**50 檔全部列出**(節錄):

| 代碼 | 名稱 | Chip 分 | Chip 評語 |
|---|---|---|---|
| 1815 | 富喬 | 20/25 | 法人近20日大買超 +11560 張,外資連賣7日(-5) |
| 2049 | 上銀 | 7/25 | 法人近20日中性 +445 張,外資連賣3日(-3) |
| 2303 | 聯電 | 0/25 | 大賣 |
| 2327 | 國巨 | 25/25 | 大買 |
| 2337 | 旺宏 | 0/25 | 大賣 |
| 2344 | 華邦電 | 0/25 | 大賣 |
| 2408 | 南亞科 | 25/25 | 大買 +54695 張 |
| 8150 | 南茂 | 25/25 | 大買 +13133 張 |

---

## 不可動範圍清單

下列邏輯在 Vanessa 明確指示前,**完全禁止更動**:

1. `money_health.py` 的 `chip_score()` 函數分級規則
2. `money_health.py` 的 `recompute_health_score()` v3 公式四項分數權重
3. `server.py` 的 `/api/money_health` 端點讀 `config.UNIVERSE` 50 檔(不傳 watchlist_codes)
4. `server.py` 的 `engine.build_state()` 呼叫改成不傳入(避免被 total_volume 過濾)
5. `health_history.py` 整檔
6. `index.html` 的 `renderHealth()` 函數(獨立 tab 頁渲染)

**允許動的範圍**:
- `reports/health_score_history/` 下的每日 snapshot JSON(每日自動寫入)
- 新增其他 tab 的 render 函數
- UI 純顯示樣式調整(不動 render 結構)
- 修 hotfix(compile crash 等例外情況)

---

## 為何凍版

- **模型驗證需要穩定基準**:30 天命中率統計必須建立在固定邏輯上,中途改公式會污染驗證結果
- **已完成驗證**:Vanessa 7/9 22:25 明確確認 v3 邏輯沒問題、UI 對齊規格
- **歷史紀錄需求**:Vanessa 要求「每日的紀錄都要存檔」、「一個月後驗收」,凍版保證模型公式不被回溯修改

---

**最後修改**:2026-07-09 22:25 by Mavis  
**下次檢視時機**:2026-07-10 收盤後(15:05 之後)看命中率首日資料

---

## Commit 紀錄

v3 程式碼已 commit 在 `c80d2fa chore(vps): 加 docker-compose / nginx config / health_history / indicators 給 VPS 部署用`(2026-07-09 22:19)
- 4 個檔 + snapshot 全部上 HEAD + origin/main
- 本檔(freeze 宣告)由 Mavis 補 commit 進去