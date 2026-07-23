# MLS v4.0 — 盤後決策系統

資金健康度為核心的台股盤後決策系統。獨立完整，不依賴外部舊模組。

## 特點

- **穩定優先**：未接 API 時以 demo 模式啟動，系統照常運作、網頁可看；接上金鑰後自動切真實資料。
- **拼圖架構**：各模組只透過 SQLite 狀態表溝通，互不 import，單一模組故障不影響其他。
- **崩潰復原**：狀態落地 DB，重啟從 DB 還原，資訊永不歸零。
- **五模組健康分**：技術/資金/籌碼/族群/承接品質，每個健康分可逐項公式驗算。
- **四關漏斗**：資金流向→法人未斷→大戶未離→承接品質，三態邏輯（PASS/FAIL/NO_DATA）。
- **驗證閉環**：觀察清單隔日自動驗證，累積分軌勝率、四象限、分數區間統計。

## 快速部署（VPS）

```bash
# 1. 上傳並解壓到 /opt/mls-v4
cd /opt && unzip mls-v4.zip

# 2. 建虛擬環境裝依賴
cd /opt/mls-v4
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. 設定環境變數
cp .env.example .env
nano .env          # 填入 Shioaji / FinMind 金鑰；未填則 demo 模式

# 4. 先手動測試啟動（demo 模式即可看畫面）
cd app
../venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
# 瀏覽器開 http://<VPS_IP>:8000

# 5. 設為系統服務
cp deploy/mls-v4.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mls-v4
systemctl status mls-v4
```

## 接上真實 API

編輯 `.env`：
```
MLS_DATA_MODE=real
SHIOAJI_API_KEY=你的金鑰
SHIOAJI_SECRET_KEY=你的密鑰
FINMIND_TOKEN=你的token
```
重啟：`systemctl restart mls-v4`

系統會自動切換到真實資料。若某資料源失敗，該部分自動降級 demo，不會整個崩。

## API 端點

| 端點 | 說明 |
|---|---|
| `GET /` | 前端（盤後模式四分頁）|
| `GET /api/health` | 系統健康檢查 |
| `GET /api/eod` | 盤後個股決策（完整五模組）|
| `GET /api/radar` | 抗跌股雷達 |
| `GET /api/watchlist` | 觀察清單 |
| `GET /api/funnel` | 漏斗四關明細 |
| `GET /api/stats` | 統計驗證 |
| `GET /api/livermore?code=XXXX` | 李佛摩六欄 |
| `GET /api/reports` | 報告庫 |
| `POST /api/run-eod` | 手動觸發盤後（補跑用）|

## 排程

系統內建 APScheduler，週一至五 15:05 自動跑盤後蓋章 + 產報告。

## 目錄結構

```
mls-v4/
├── app/
│   ├── main.py          FastAPI 主程式 + API 路由 + 排程
│   ├── config.py        全域設定 + 觀察池
│   ├── db.py            SQLite 封裝 + 建表 + 崩潰復原
│   ├── broker.py        Shioaji 封裝（可降級 demo）
│   ├── chips.py         FinMind 籌碼封裝（可降級 demo）
│   ├── decision.py      五模組健康分 + 象限 + 分級（真實公式）
│   ├── absorption.py    承接品質第五模組
│   ├── funnel.py        四關漏斗
│   ├── after_hours.py   盤後流水線 + 觀察清單 + 驗證 + 統計
│   ├── livermore.py     李佛摩六欄狀態機
│   ├── report.py        日報/週報產生
│   ├── web/index.html   前端（吃 API）
│   └── data/            SQLite + 報告庫（執行時生成）
├── deploy/mls-v4.service
├── requirements.txt
├── .env.example
└── README.md
```

## 資料階段鐵律

- `intraday_est`：盤中估算值（主動買賣差非法人，流出≠派發）
- `eod_final`：盤後蓋章真值（法人/融資/大戶確定）

本系統為**盤後模式**。盤中模式（市場走向儀表）為後續擴充。
