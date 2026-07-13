# 2026-07-09 收盤後 — Hotfix Freeze 紀錄

**狀態**:🟢 已驗證、可運作 — **凍版,禁止更動**  
**版本**:scoring.py hotfix rev.2026-07-09  
**部署**:`/Users/vanessaliu/Desktop/01_投資分析/MLS 完整系統 v1.2 /MLS整系統最終版v1.2/`

---

## ⚠️ 不可動原則(Hard Rule)

本檔描述的任何 hotfix 改動,**未經 Vanessa 明確口頭/文字核准前,不可更動**。
違反這條 = 直接退回上版 + 立刻回報 Vanessa。

例外:
- 修到不能 compile / 完全 crash 主流程
- Vanessa 主動指示修改

---

## 改動清單(只動 scoring.py 一個檔)

### Hotfix 1 — volume 因子加階梯(scoring.py:298-308)
**問題**:2026-07-09 大盤驗證,5 檔全 tnvr 1.06~1.21 落在 1.3 門檻下,volume 因子永遠 0,拖累整體 score。
**改動**:加 `tnvr >= 1.0 → F["volume"] = 6` 階梯。
**驗證**:5 檔 volume 從 0 → 6。
**長期方案**(待 Phase A):改成全市場 TNVR 百分位排名,Top 30% 拿高分。

### Hotfix 2 — chip 缺資料降階(scoring.py:313-321)
**問題**:原本 `if chip:` 缺資料直接跳過整個區塊。
**改動**:chip dict 缺有效值時給 baseline 3 分(中性,避免誤判為弱)。
**驗證**:略(實測 2337/2344 因法人大賣 chip 真實 0 分,baseline 沒生效;副作用記在下方)。

### Hotfix 3 — divergence() 邊界保護(scoring.py:248-258)
**問題**:`aflow=None` 會 TypeError crash,主流程雖然 try/except 包,但模擬測試或未來 refactor 會暴露。
**改動**:`aflow is None or not total_volume` 直接回 None,不算 ratio。

---

## 副作用清單(已知,凍版期不修)

### 副作用 A — 2337/2344 chip 仍 0
**真相**:FinMind cache 沒資料是**假議題**,實際這兩檔 7/9 法人真的大賣 9 萬張以上,
chip 條件 `inst_net_20d_lots > 0` 不該觸發,所以 chip=0 是正確判定。
Hotfix 2 的 baseline 3 分**沒生效**是因為 chip dict 有資料(只是值為負)。
**意義**:凍版期不修。若要修,改成「賣超顯著時額外扣分」,而非「缺資料加分」。

### 副作用 B — 漲停股(tnvr<1.3)現在拿 6 分,可能讓 borderline 股票誤觸 buy
**範圍**:所有 tnvr 在 1.0~1.3 的盤中股票。
**風險**:明日 7/10 若 lock 族群擴大,可能誤觸幾支溫和量股。
**對策**:凍版期只看,7/10 收盤後檢視 false positive 數量再決定要不要回滾 Hotfix 1。

### 副作用 C — _entry_min 動態門檻持續 from db
**現況**:db kv `entry_score_min = 46`(預設 40,被 after_hours 動態調過)。
**行為**:任何 score < 46 都進不了 buy 主路徑。
**處置**:凍版期間不動 db。

---

## 不可動範圍清單

下列檔案/邏輯在 Vanessa 明確指示前,**完全禁止更動**:
1. `scoring.py` 整檔(本次 hotfix 已 commit 進去)
2. `engine.py` 進場條件分支(line 326-343,`_entry_min()` 引用鏈)
3. `gatekeeper.py` 持股 → action=hold 邏輯
4. `db.py` 的 `kv_get('entry_score_min')` 預設值與 after_hours 動態調整
5. `config.py` 的 `R005_VR / R006_VR / LONE_WOLF_PCT / MIN_VOLUME_LOTS` 4 個常數
6. `broker.py` 的 snapshot 欄位 mapping

**允許動的範圍**:
- 新增 endpoint(只加不動舊的)
- 新增 plugin(money_health / strategy_doc / nexora 等)獨立模組
- reports/ 與 scripts/ 底下純分析腳本
- index.html / rankings.html UI 顯示層

---

## 驗證快照(2026-07-09 收盤後 18:32~20:59)

| 股票 | 修前 ai/action | 修後 ai/action | bs_pass | 漲幅 |
|---|---|---|---|---|
| 2408 南亞科 | 58/hold | **64/hold** | True | +9.97% 漲停(持股內 hold) |
| 8150 南茂 | 46/buy | **52/buy** | True | +9.85% 漲停 |
| 6182 合晶 | 36/watch | **42/watch** | False | +7.81%(賣壓重,合理 watch) |
| 2337 旺宏 | 16/watch | **22/watch** | False | +3.61%(法人賣超 9.2 萬張) |
| 2344 華邦電 | 16/watch | **22/watch** | False | +4.74%(法人賣超 9.9 萬張) |

**結論**:修後判定**與市場實況一致**。
- 漲停 2 檔:1 hold(持股)、1 buy(進場) ✅
- 法人大賣 2 檔:watch 不進場 ✅
- 賣壓重 1 檔:watch ✅

---

## 為何 7/10 之後暫不開新盤

凍版期間(2026-07-09 收盤後 → Vanessa 解禁):
- 系統觀察 hotfix 在 7/10 的表現
- 收盤後檢視 false positive / false negative 比例
- Vanessa 拍板後再進下一輪優化

---

**最後修改**:2026-07-09 22:25 by Mavis  
**下次檢視時機**:2026-07-10 收盤後(15:05 之後)

---

## Commit 紀錄

```
573b954 fix(scoring): hotfix rev.2026-07-09 — volume 1.0 階梯 + chip baseline + divergence 邊界
- 2 files changed, 539 insertions(+)
- 建立方式:內層 MLS整系統最終版v1.2/ 獨立 git init(與外層 v1.2 root repo 脫鉤)
- 只 add 我這輪自己改的兩個檔,其他 60+ 檔保持 untracked
```

---

## Deploy 紀錄(本地端)

- Server PID 49983 跑在 port 8000(2026-07-09 20:09 啟動)
- Curl `/api/state` → HTTP 200 ✅
- ⚠ **Shioaji session 在 21:09 後斷線**(server log: `SolClient send request ... code: NotReady`)。
  - 影響:盤中 snapshot 抓不到,`/api/state` 仍 200 但 stocks 為空
  - **不動**(凍版),等 Vanessa 決定是否重啟 broker 或檢查 Shioaji 連線
  - 推測:永豐 API session timeout,需重 login 或延長心跳
- Vercel 部署(`mls-v1-2.vercel.app`)需 Vanessa 親自 `vercel deploy --prod --yes` 觸發,本次**未自動 deploy**