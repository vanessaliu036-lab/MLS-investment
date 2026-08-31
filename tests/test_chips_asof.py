"""籌碼資料日回歸測試。"""

import json
import sys
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1] / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(MODULE_DIR))

import chips  # noqa: E402


def _row(date, name, buy, sell):
    return {"date": date, "name": name, "buy": buy, "sell": sell}


def test_official_cache_uses_latest_date_on_or_before_asof(monkeypatch, tmp_path):
    cache = tmp_path / "chips_cache.json"
    cache.write_text(json.dumps({
        "stocks": {
            "2330": {
                "source_date": "2026-08-28",
                "inst_net_20d_lots": 100,
                "foreign_net_20d": 80,
                "trust_net_20d": 10,
                "dealer_net_20d": 10,
                "inst_streak": 2,
            }
        }
    }), encoding="utf-8")
    monkeypatch.setattr(chips, "CACHE_FILE", str(cache))

    result = chips._official_detail("2330", asof="2026-08-30")

    assert result["source_date"] == "2026-08-28"


def test_each_chip_source_keeps_its_own_latest_available_date(monkeypatch, tmp_path):
    cache = tmp_path / "chips_cache.json"
    cache.write_text('{"date":"","stocks":{}}', encoding="utf-8")
    monkeypatch.setattr(chips, "CACHE_FILE", str(cache))
    monkeypatch.setattr(chips, "_cache", {"date": "", "stocks": {}})
    monkeypatch.setattr(chips, "_today_key", lambda: "2026-08-31")
    monkeypatch.setattr(chips, "_save_disk", lambda: None)

    def fake_finmind(dataset, code, start_date):
        if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
            return [
                _row("2026-08-28", "Foreign_Investor", 4000, 1000),
                _row("2026-08-31", "Foreign_Investor", 9000, 0),
            ]
        if dataset == "TaiwanStockMarginPurchaseShortSale":
            return [{
                "date": "2026-08-27",
                "MarginPurchaseTodayBalance": 100,
                "MarginPurchaseYesterdayBalance": 95,
                "ShortSaleTodayBalance": 20,
                "ShortSaleYesterdayBalance": 18,
            }, {
                "date": "2026-08-31",
                "MarginPurchaseTodayBalance": 999,
                "MarginPurchaseYesterdayBalance": 1,
                "ShortSaleTodayBalance": 999,
                "ShortSaleYesterdayBalance": 1,
            }]
        if dataset == "TaiwanStockSecuritiesLending":
            return [{"date": "2026-08-26", "volume": 5000},
                    {"date": "2026-08-31", "volume": 9000}]
        if dataset == "TaiwanDailyShortSaleBalances":
            return [{
                "date": "2026-08-26",
                "SBLShortSalesCurrentDayBalance": 4000,
                "SBLShortSalesPreviousDayBalance": 3000,
            }, {
                "date": "2026-08-31",
                "SBLShortSalesCurrentDayBalance": 9000,
                "SBLShortSalesPreviousDayBalance": 1,
            }]
        if dataset == "TaiwanStockShareholding":
            return [{
                "date": "2026-08-29",
                "ForeignInvestmentSharesRatio": 10.0,
                "ForeignInvestmentRemainRatio": 20.0,
            }]
        return []

    monkeypatch.setattr(chips, "_finmind", fake_finmind)

    result = chips.get_chips_detail("2330", asof="2026-08-30")

    assert result["source_date"] == "2026-08-28"
    assert result["margin_source_date"] == "2026-08-27"
    assert result["margin_change_d"] == 5
    assert result["lending_source_date"] == "2026-08-26"
    assert result["lending_volume_d"] == 5
    assert result["lending_balance_change_d"] == 1
    assert result["foreign_share_source_date"] == "2026-08-29"


def test_decision_ui_exposes_chip_data_date_separately_from_quote_date():
    html_path = Path(__file__).resolve().parents[1] / "5483_中美晶_個股決策UI.html"
    html = html_path.read_text(encoding="utf-8")
    standalone_path = Path(__file__).resolve().parents[1] / "個股籌碼獨立UI.html"
    standalone = standalone_path.read_text(encoding="utf-8") if standalone_path.exists() else ""
    server = (Path(__file__).resolve().parents[1] / "個股卡片相關檔案_20260722" / "server.py").read_text(encoding="utf-8")

    assert 'id="chip-data-date"' in html
    assert "chip_data_date" in html
    assert "盤中即時行情" in html
    assert 'href="/chips"' in html
    assert 'target="_blank"' in html
    assert "籌碼資料日" in standalone
    assert "api/stock/" in standalone
    assert 'class="stock-menu"' in standalone
    assert "position:fixed" in standalone
    assert "api/watchpool" in standalone
    assert "51 檔" in standalone
    assert '@app.get("/chips")' in server
