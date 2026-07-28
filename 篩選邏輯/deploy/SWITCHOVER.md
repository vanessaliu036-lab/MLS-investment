# 正式站接管：decision_v22(momentum) → 篩選邏輯(雙鏈)

> 2026-07-28 定案：舊 momentum 模型判斷矛盾，正式站改跑 `篩選邏輯/api:app`。
> **這一步會動到正式站 66.42.42.150:8000，且與現行服務搶同一個 8000 埠 —— 不可逆，
> 請在收盤後、確認下列每一步後手動執行。**

## 為什麼要小心

- 現行正式站 = `mls-intraday.service`（跑 `個股卡片相關檔案_20260722/server.py`，decision_v22）綁 **8000**。
- 新服務 = `mls-screen-api.service`（跑 `篩選邏輯/api:app`）也綁 **8000**。
- 兩者不能同時開 → 必須先停舊、再上新。

## 接管步驟（VPS，root，收盤後）

1. **同步新程式**（含本次 A鏈/B鏈/verify 全部）
   ```bash
   rsync -a --exclude '.env' --exclude '*.db' --exclude '*.db-*' \
         篩選邏輯/ root@66.42.42.150:/opt/mls-screen/
   ```
   （資料檔排除，避免蓋掉線上 DB；憑證 `.env` 另放，見下。）

2. **憑證 + 套件**（首次才需要）
   ```bash
   # /opt/mls-screen/.env 放 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY / FINMIND_TOKEN
   pip install fastapi uvicorn shioaji
   ```

3. **先灌資料再切**（否則名單全空）—— 用新族群 51 檔補上一個交易日的盤後
   ```bash
   cd /opt/mls-screen && python3 calendar_sync.py
   python3 collect.py --date <上一交易日>   # 例 2026-07-27，落地 watchlist_post/candidate_pool
   python3 preflight.py                       # 六項全過才繼續
   ```

4. **停舊、上新**（搶埠切換，這一刻起正式站換模型）
   ```bash
   systemctl disable --now mls-intraday.service        # 停 decision_v22
   cp /opt/mls-screen/deploy/mls-screen-*.service /etc/systemd/system/
   cp /opt/mls-screen/deploy/mls-screen-*.timer   /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now mls-screen-api.service       # 上 篩選邏輯
   systemctl enable --now mls-screen-collect.timer     # 週一~五 13:40 盤後採集
   systemctl enable --now mls-screen-feed.timer        # 週一~五 08:55 Shioaji 訂閱
   systemctl enable --now mls-screen-calendar.timer    # 週一 06:00 假日表
   ```

5. **驗收**
   ```bash
   curl -s localhost:8000/api/phase              # 回當下時段(休市=CLOSED)
   curl -s localhost:8000/api/watchlist | head   # A鏈名單
   curl -s localhost:8000/api/pool/tomorrow | head   # 隔日候選池
   systemctl list-timers 'mls-screen-*'
   ```

## 回滾（新服務有問題時）

```bash
systemctl disable --now mls-screen-api.service
systemctl enable  --now mls-intraday.service   # 切回 decision_v22
```

## 切換後的端點對照（前端 UI 分開）

| 用途 | 端點 |
|---|---|
| A鏈即時名單(盤前/盤中嚴判/盤後寬篩) | `/api/watchlist?phase=` |
| B鏈盤中發現（收盤後才有料） | `/api/b/discovery`、`/api/b/scan`(13:20)、`/api/b/verify`(13:31) |
| 隔日盤中候選池（A∪B 匯流） | `/api/pool/merge`、`/api/pool/tomorrow` |
| 命中率/勝率復盤 | `/api/verify/run`、`/api/verify/stats?days=30` |

## 排程補完整條鏈（B鏈 + 復盤，目前 timer 只有 collect/feed/calendar）

現行 `mls-screen-collect.timer`(13:40) 只跑 A鏈盤後採集。要讓 B鏈與復盤自動化，
在盤後序列補上（可加進 `run_collect.sh` 尾端，或另建 timer）：
```
# 13:20  B鏈最終掃描（需盤中 b_snapshot 已在跑）
python3 -c "import b_discover,config; b_discover.scan(config.UNIVERSE, config.CODE_GROUP)"
# 13:31 後  B鏈法人驗證 + A/B 匯流
python3 -c "import merge_pool; merge_pool.merge()"
# 盤後  當日收盤復盤（用今日收盤驗昨日候選池）
python3 -c "import screen_verify; screen_verify.verify()"
```
且盤中 `intraday_feed.py` 的 Shioaji handler 需每 5 分呼叫 `b_snapshot.take(buffer)`
（B鏈快照的資料源）—— 這是 B鏈能出料的前提，屬盤中接線，未接則 B鏈恆空、A鏈照跑。
