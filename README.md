# nexora

**AI Operating System** — 整合個人助理、投資情報、自主代理、知識管理於一體的模組化平台。

> 願景：把多套 AI 工具收進同一個作業系統底層，模組化、可組合、可部署。
> 現有模組：`mls-investment`（台股盤中決策系統）。

---

## 模組總覽

| 模組 | 狀態 | 說明 |
|---|---|---|
| `mls-investment` | 🟢 v1.2 | 台股盤中決策系統：固定觀察池 → 板塊 → 龍頭 → 規則引擎 → 熱力表 |

每個模組獨立可部署，透過共用資料層（SQLite / Airtable）與推播層（Telegram）串接。

---

## mls-investment

台股盤中決策系統。固定觀察池 → 資金流入板塊 → 龍頭深度分析 → 規則引擎熱力表，附現金閘門、回撤斷路器、四象限輪動與權重自學習迴圈。

部署、API、鐵則、每日閉環請見 **[HANDOFF.md](./HANDOFF.md)**。

### 快速啟動

```bash
pip install -r requirements.txt
cp .env.example .env          # 填入 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY
cp positions.json.example positions.json   # 填入實際持股
python backtest.py            # 預訓練因子權重與門檻(首次)
python server.py              # → http://127.0.0.1:8000
```

詳見 `HANDOFF.md` 的「部署檢查清單」。