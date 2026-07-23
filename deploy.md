# mls-intraday VPS 部署 SOP（66.42.42.150:8000）

**注意**：此 IP 與既有 pos-v4.0（`104.156.239.83`）**不是同一台**。

## 現況盤點（2026-07-22）

| 項目 | 狀態 |
|---|---|
| `mls-intraday/` | ✅ git repo 已 init（剛做） |
| `個股卡片相關檔案_20260722/` | ✅ 51 檔 SECTOR_MAP 在此 |
| `app/` | ✅ 篩選公式 + 訂閱骨架 |
| `vps_intraday_test.py` | ✅ `/intraday-test` 路由在此 |
| VPS `66.42.42.150` | ⚠️ 從未 ssh 過、host key 未 trust |
| `.ssh/known_hosts` | ⚠️ 沒有 66.42.42.150 記錄 |

## 部署源選擇 ✅ 已定案 = A

| 選項 | 路徑 | 優 | 缺 |
|---|---|---|---|
| **A. 整包** | `~/Desktop/mls-intraday/`（含 0722 快照） ✅ | 一次到位、/intraday-test 可用 | 0722 是快照、可能跟主線漂移 |
| B. 主線 + 補 config | `app/` + 補 `config.py` | 維護單一 source of truth | 缺 server.py、無法跑主 server |
| C. 只 0722 | `個股卡片相關檔案_20260722/` | server + config 同源 | 缺 vps_intraday_test.py、/intraday-test 會 disabled |

**2026-07-23 09:11 Vanessa 拍板：選 A**。詳見 commit `97c6386 feat: import full source tree`。

## 一鍵部署（推薦）

```bash
cd ~/Desktop/mls-intraday
bash deploy_vps.sh
```

`deploy_vps.sh` 會自動跑 6 步：
1. SSH 連通測試（首次自動 trust host key）
2. 備份現有部署到 `.bak.${TIMESTAMP}`
3. rsync 源碼到 `/opt/mls-intraday`（已 exclude `.git` `.env` `*.db` `*.bak`）
4. 推送 `.env`（chmod 600，不印內容）
5. 安裝 pip 依賴 + 啟動 uvicorn（背景跑、log 在 `/tmp/mls-intraday.log`）
6. 跑 6 項驗證（HTTP code、51 檔、引擎成員、UI 標題、log）

## 部署流程（Vanessa 手動跑）

### 1. 確認 SSH 可通

```bash
ssh root@66.42.42.150 "echo ok && uname -a"
# 第一次會問 host key，確認指紋後輸入 yes
```

### 2. 確認 VPS 環境

```bash
ssh root@66.42.42.150 "python3 --version && which uvicorn && pip3 list 2>/dev/null | grep -iE 'fastapi|uvicorn|shioaji|pandas'"
```

需求：Python 3.10+、FastAPI、uvicorn、shioaji、pandas、python-dotenv。
如果沒有就 `pip3 install -r requirements.txt`。

### 3. rsync 源碼

```bash
# 備份先（如果 VPS 上已有舊版）
ssh root@66.42.42.150 "[ -d /opt/mls-intraday ] && mv /opt/mls-intraday /opt/mls-intraday.bak.\$(date +%Y%m%d) || true"

# 推主線 + 0722 快照 + vps_intraday_test.py
rsync -avz --delete \
  -e ssh \
  ~/Desktop/mls-intraday/ \
  root@66.42.42.150:/opt/mls-intraday/
```

`--delete` 必帶（不然舊 .bak / .pyc 殘留）。

### 4. .env 推上去

```bash
# 主機有 .env → 推到 VPS
scp ~/Desktop/mls-intraday/.env root@66.42.42.150:/opt/mls-intraday/.env
chmod 600 /opt/mls-intraday/.env
```

⚠️ **不要 echo .env 內容**。驗證用 `wc -c` 確認大小。

### 5. 啟動 server（背景跑）

```bash
ssh root@66.42.42.150 "cd /opt/mls-intraday/個股卡片相關檔案_20260722 && nohup python3 -m uvicorn server:app --host 0.0.0.0 --port 8000 > /tmp/mls-intraday.log 2>&1 &"
```

`server.py:886` 已寫死 `host=0.0.0.0, port=8000`，可省略 `--host/--port`。

### 6. 驗證（6 項）

```bash
# 6.1 主路由活
curl -s -o /dev/null -w "%{http_code}\n" http://66.42.42.150:8000/
# 預期 200

# 6.2 /intraday-test 路由活
curl -s -o /dev/null -w "%{http_code}\n" http://66.42.42.150:8000/intraday-test
# 預期 200（vps_intraday_test.py 必須跟著上傳）

# 6.3 51 檔全到
curl -s http://66.42.42.150:8000/intraday-test/api/stocks | python3 -c "import json,sys; d=json.load(sys.stdin); print('codes:', len(d.get('codes',[]))); print('first 5:', d.get('codes',[])[:5])"
# 預期 codes: 51

# 6.4 引擎成員正確
curl -s http://66.42.42.150:8000/intraday-test/api/stocks | python3 -c "import json,sys; d=json.load(sys.stdin); engines=d.get('engine_stocks',[]); print('engine:', engines); assert set(engines)=={'2303','5347'}, 'engine stocks wrong'"
# 預期 engine: ['2303', '5347']

# 6.5 UI 可開
curl -s http://66.42.42.150:8000/intraday-test/ | grep -oE "<title>[^<]+</title>"
# 預期看到 UI 標題

# 6.6 log 沒 OOM / 沒 import error
ssh root@66.42.42.150 "tail -30 /tmp/mls-intraday.log | grep -iE 'error|trace|exception' || echo 'log clean'"
```

## 回滾

```bash
ssh root@66.42.42.150 "pkill -f 'uvicorn server:app' && mv /opt/mls-intraday.bak.YYYYMMDD /opt/mls-intraday && cd /opt/mls-intraday/個股卡片相關檔案_20260722 && nohup python3 -m uvicorn server:app --host 0.0.0.0 --port 8000 > /tmp/mls-intraday.log 2>&1 &"
```

## mavis 邊界

- ❌ mavis 不跑 ssh / rsync / uvicorn
- ✅ mavis 可跑 `git add+commit`（imperative, 預覽 status + diff）
- ✅ mavis 可改 source code（imperative, 預覽 diff）
