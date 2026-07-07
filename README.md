# MLS Standard v1.2

台股盤中決策系統：固定觀察池 → 資金流入板塊 → 龍頭深度分析 → 規則引擎熱力表，附現金閘門、回撤斷路器、四象限輪動與權重自學習迴圈。

部署、API、鐵則、每日閉環請見 **[HANDOFF.md](./HANDOFF.md)**。

## 快速啟動

```bash
pip install -r requirements.txt
cp .env.example .env          # 填入 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY
cp positions.json.example positions.json   # 填入實際持股
python backtest.py            # 預訓練因子權重與門檻(首次)
python server.py              # → http://127.0.0.1:8000
```

詳見 `HANDOFF.md` 的「部署檢查清單」。