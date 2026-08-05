# 部署與排程

三支排程 + 一支常駐 API。**時間觸發只是粗篩(週一~五),真正的交易日/假日判斷在 app 層**
(`phase.py` 讀 `holidays.json`),所以國定假日就算 timer 照響,`collect.py` / `intraday_feed.py`
也會自己偵測並退出,不會亂打 API、不會亂連 Shioaji。

## 元件

| 單元 | 何時 | 做什麼 |
|---|---|---|
| `mls-screen-api.service` | 常駐 | uvicorn 提供 `/api/watchlist` 唯一名單端點 |
| `mls-screen-collect.timer` | 週一~五 14:40 | 盤後採集 51 檔 + A 鏈收盤復盤 + B 鏈驗證/匯流 |
| `mls-screen-feed.timer` | 週一~五 08:55 | 拉起盤中 Shioaji 訂閱,寫 `quote_snap`/`aflow`,收盤自停 |
| `mls-screen-calendar.timer` | 週一 06:00 | 更新 TWSE 官方假日 → `holidays.json`(跨年自動補) |

## 安裝(VPS,root)

1. 放程式(假設放 `/opt/mls-screen`,與 unit 檔內路徑一致;不同就改 unit 的 `WorkingDirectory` 與 `ExecStart`):
   ```bash
   rsync -a 篩選邏輯/ root@VPS:/opt/mls-screen/
   ```
2. 憑證:把 `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY` / `FINMIND_TOKEN` 放進 `/opt/mls-screen/.env`
   (run 腳本會自動 source;**不要**寫進 unit 檔或 git)。
3. 裝套件:
   ```bash
   pip install fastapi uvicorn shioaji
   ```
4. 先補一次假日表 + 一次歷史採集(否則盤前名單是空的):
   ```bash
   cd /opt/mls-screen && python3 calendar_sync.py && python3 collect.py --date <上一交易日>
   ```
5. 裝 systemd:
   ```bash
   cp /opt/mls-screen/deploy/mls-screen-*.service /etc/systemd/system/
   cp /opt/mls-screen/deploy/mls-screen-*.timer   /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now mls-screen-api.service
   systemctl enable --now mls-screen-collect.timer
   systemctl enable --now mls-screen-feed.timer
   systemctl enable --now mls-screen-calendar.timer
   ```

## 檢查

```bash
systemctl list-timers 'mls-screen-*'      # 看下次觸發時間
journalctl -u mls-screen-collect -n 50    # 看盤後採集日誌
journalctl -u mls-screen-feed -f          # 盤中即時看行情 flush
curl -s localhost:8000/api/phase          # 看當下時段(休市會回 CLOSED)
```

## 手動補跑

```bash
python3 collect.py --date 2026-07-24      # 補特定交易日盤後名單
python3 intraday_feed.py --selftest       # 不連 Shioaji,驗證 quote/aflow 落地路徑
```

## 本機(macOS)開發

不需要 systemd。開一個 API 看畫面即可:
```bash
cd 篩選邏輯 && python3 -m uvicorn api:app --port 8011
```
排程用 `crontab -e` 加(等同上面三個 timer):
```
40 14 * * 1-5  cd /path/篩選邏輯 && ./deploy/run_collect.sh
55 8  * * 1-5  cd /path/篩選邏輯 && ./deploy/run_feed.sh
0  6  * * 1    cd /path/篩選邏輯 && ./deploy/run_calendar.sh
```
