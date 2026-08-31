# MLS 部署操作環境與 AI 交接手冊

> 本文件是部署的單一入口。任何 AI 或工程師接手前，先讀完本文件，再讀 `deploy.md` 的歷史背景。
>
> 建立日期：2026-08-31。文件不保存任何密碼、API token、SSH private key 或 `.env` 內容。

## 1. 專案與主機

| 項目 | 正式設定 |
|---|---|
| GitHub Repo | `https://github.com/vanessaliu036-lab/MLS-investment` |
| 本機 Repo | `/Users/vanessaliu/Desktop/mls-intraday` |
| VPS | `root@66.42.42.150` |
| 主服務目錄 | `/opt/mls-intraday` |
| 主服務 | `mls-intraday.service` |
| 主服務 Port | `8000` |
| AB 引擎目錄 | `/opt/mls-screen` |
| AB 引擎服務 | `mls-ab-engine.service` |
| AB 引擎 Port | `8002` |

正式站：<http://66.42.42.150:8000/>

## 1.1 本次交付識別

| 項目 | 值 |
|---|---|
| 籌碼修正分支 | `fix/chips-use-latest-trading-day` |
| 籌碼修正 commit | `34aca68` |
| 自動部署設定 commit | `7f0c152` |
| 自動部署設定檔 | `deploy-vps.yml`、`ci_deploy_vps.sh`、`GITHUB_VPS_SETUP.md` |

如果本機 checkout 找不到上述分支或 commit，先停止並定位正確 worktree；不可從其他分支重建同名分支來代替。

## 1.2 GitHub Actions Secrets

設定位置：

```text
GitHub Repo
→ Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

必須建立以下 5 個 Repository secrets：

| Secret 名稱 | 值／用途 |
|---|---|
| `VPS_HOST` | `66.42.42.150` |
| `VPS_USER` | `root` |
| `VPS_PORT` | `22` |
| `VPS_SSH_KEY` | VPS 部署用 SSH private key；只放 GitHub Secret |
| `VPS_KNOWN_HOSTS` | VPS host key；只放 GitHub Secret |

`VPS_SSH_KEY` 與 `VPS_KNOWN_HOSTS` 絕對不可貼到對話、寫入 `.md`、commit、workflow log 或任何 Repo 檔案。

自動部署檔若尚未出現在本機 checkout，應先確認正確 branch／commit，再把檔案放入 Repo；不可自行產生另一套 workflow 覆蓋原設定。

## 2. 重要程式路徑

### 主服務（Port 8000）

主服務的個股卡片與官方籌碼模組位於：

```text
/opt/mls-intraday/個股卡片相關檔案_20260722/
```

主要檔案：

```text
chips.py
official_source.py
chips_official.py
server.py
chips_cache.json       # 執行期資料，不從本機覆蓋
```

### AB 引擎（Port 8002）

```text
/opt/mls-screen/
```

`篩選邏輯/` 內的篩選與七層狀態程式，依目前服務設定由 `/opt/mls-screen` 提供。

## 3. Git 前置檢查

不要猜分支名稱，也不要在尚未確認 commit 時推送或部署：

```bash
cd /Users/vanessaliu/Desktop/mls-intraday
git branch --show-current
git rev-parse HEAD
git remote -v
git status --short
```

推送前必須確認：

1. 目前分支就是本次要交付的分支。
2. `HEAD` 是本次要上傳的 commit。
3. 工作樹中與本次無關的修改不會被加入 commit。
4. GitHub 認證已就緒，但認證資訊不可寫入 Repo。

推送目前分支：

```bash
git push -u origin "$(git branch --show-current)"
```

若本機找不到指定 commit 或分支，先停止，不要從其他分支猜測或複製檔案。

## 4. 安全部署原則

### 絕對不要上傳

```text
.env
*.db
*.db-wal
*.db-shm
chips_cache.json
shioaji.log
*.bak*
.git/
```

`.env` 只應存在於 VPS 的既有安全位置；除非 Vanessa 明確授權，不得以 `scp`、`rsync` 或任何部署腳本上傳。

不要直接使用 `rsync --delete` 覆蓋整個 VPS，也不要把本機未提交的工作樹當成正式版本。

### 單檔部署流程

先備份 VPS 原檔，再只同步已確認的正式程式檔：

```bash
VPS=root@66.42.42.150
REMOTE_DIR=/opt/mls-intraday/個股卡片相關檔案_20260722

ssh "$VPS" 'd="/opt/mls-intraday/個股卡片相關檔案_20260722"; stamp="manual-$(date +%Y%m%d%H%M%S)"; cp -a "$d/chips.py" "$d/chips.py.bak.$stamp"; cp -a "$d/official_source.py" "$d/official_source.py.bak.$stamp"'

scp 個股卡片相關檔案_20260722/chips.py \
  "$VPS:$REMOTE_DIR/chips.py"
scp 個股卡片相關檔案_20260722/official_source.py \
  "$VPS:$REMOTE_DIR/official_source.py"

ssh "$VPS" 'cd "/opt/mls-intraday/個股卡片相關檔案_20260722" && python3 -m py_compile chips.py official_source.py && systemctl restart mls-intraday.service && systemctl is-active mls-intraday.service'
```

若修改的是 AB 引擎檔案，目標改為 `/opt/mls-screen/`，完成後重啟 `mls-ab-engine.service`；不要因此重啟無關服務。

## 5. 部署後驗證

```bash
ssh root@66.42.42.150 'systemctl is-active mls-intraday.service; systemctl is-active mls-ab-engine.service'

curl -sS -o /dev/null -w '%{http_code}\n' http://66.42.42.150:8000/
curl -sS -o /dev/null -w '%{http_code}\n' http://66.42.42.150:8000/opportunity-ledger
curl -sS -o /dev/null -w '%{http_code}\n' http://66.42.42.150:8000/line-b-ledger
curl -sS -o /dev/null -w '%{http_code}\n' http://66.42.42.150:8000/line-b-layers
curl -sS -o /dev/null -w '%{http_code}\n' http://66.42.42.150:8000/watch-first-layer
curl -sS -o /dev/null -w '%{http_code}\n' 'http://66.42.42.150:8000/api/card_page?code=1815'
```

官方法人資料驗證：

```bash
curl -sS 'http://66.42.42.150:8000/api/market/official?date=YYYY-MM-DD'
```

個股籌碼的正式公開來源：

- TWSE T86：<https://www.twse.com.tw/rwd/zh/fund/T86>
- TPEx 三大法人日報：<https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading>

驗證時要記錄資料日期；週末或尚未公布當日資料時，不得把空值當成 `0`。

## 6. 回滾

部署前備份檔名會包含時間戳。發現 `py_compile`、服務狀態或 HTTP 驗證失敗時，先回復該次備份，再重啟對應服務：

```bash
ssh root@66.42.42.150 'd="/opt/mls-intraday/個股卡片相關檔案_20260722"; cp -a "$d/chips.py.bak.<timestamp>" "$d/chips.py"; cp -a "$d/official_source.py.bak.<timestamp>" "$d/official_source.py"; systemctl restart mls-intraday.service'
```

`<timestamp>` 必須替換成實際備份檔名，不可使用未確認的 glob 或刪除整個目錄。

## 7. 交接紀錄格式

每次部署完成後，在工作回報中寫明：

```text
GitHub commit：<full sha>
GitHub branch：<branch>
VPS target：<path>
Backup：<remote backup path>
Service：<service> = active
HTTP verification：<routes and status>
Official data date：<YYYY-MM-DD>
Secrets uploaded：no
```
