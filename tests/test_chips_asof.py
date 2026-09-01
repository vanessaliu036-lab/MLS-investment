"""籌碼資料日回歸測試。"""

import json
import sys
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1] / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(MODULE_DIR))

import chips  # noqa: E402
import chips_official  # noqa: E402
import extras  # noqa: E402


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
    monkeypatch.setattr(chips, "_official_margin_snapshot", lambda asof=None: {})

    def fake_finmind(dataset, code, start_date):
        if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
            return [
                _row("2026-08-28", "Foreign_Investor", 4000, 1000),
                _row("2026-08-31", "Foreign_Investor", 9000, 0),
            ]
        if dataset == "TaiwanStockMarginPurchaseShortSale":
            return []
        if dataset == "TaiwanStockSecuritiesLending":
            return [{"date": "2026-08-26", "volume": 5000},
                    {"date": "2026-08-31", "volume": 9000}]
        if dataset == "TaiwanDailyShortSaleBalances":
            return [{
                "date": "2026-08-26",
                "MarginShortSalesCurrentDayBalance": 90,
                "MarginShortSalesPreviousDayBalance": 88,
                "SBLShortSalesCurrentDayBalance": 4000,
                "SBLShortSalesPreviousDayBalance": 3000,
            }, {
                "date": "2026-08-27",
                "MarginShortSalesCurrentDayBalance": 100,
                "MarginShortSalesPreviousDayBalance": 95,
                "SBLShortSalesCurrentDayBalance": 6000,
                "SBLShortSalesPreviousDayBalance": 5000,
            }, {
                "date": "2026-08-31",
                "MarginShortSalesCurrentDayBalance": 999,
                "MarginShortSalesPreviousDayBalance": 1,
                "SBLShortSalesCurrentDayBalance": 999,
                "SBLShortSalesPreviousDayBalance": 1,
            }, {
                "date": "2026-08-25",
                "SBLShortSalesCurrentDayBalance": 4000,
                "SBLShortSalesPreviousDayBalance": 3000,
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
    list_path = Path(__file__).resolve().parents[1] / "個股籌碼清單UI.html"
    listing = list_path.read_text(encoding="utf-8") if list_path.exists() else ""
    modal_path = Path(__file__).resolve().parents[1] / "個股籌碼彈窗UI.html"
    modal = modal_path.read_text(encoding="utf-8") if modal_path.exists() else ""
    server = (Path(__file__).resolve().parents[1] / "個股卡片相關檔案_20260722" / "server.py").read_text(encoding="utf-8")
    extras = (Path(__file__).resolve().parents[1] / "個股卡片相關檔案_20260722" / "extras.py").read_text(encoding="utf-8")

    assert 'id="chip-data-date"' in html
    assert "chip_data_date" in html
    assert "盤中即時行情" in html
    assert 'href="/chips"' in html
    assert 'target="_blank"' in html
    assert "籌碼資料日" in standalone
    assert "api/chips/" in standalone
    assert "api/stock/" not in standalone
    assert 'class="back-link"' in standalone
    assert 'href="/"' in standalone
    assert 'class="chip-tab active"' in standalone
    assert "focusChipSection" in standalone
    assert 'class="sheet-bottom-actions"' not in standalone
    assert 'class="sheet-handle"' in standalone
    assert 'class="sheet-controls"' in standalone
    assert '籌碼提醒' not in standalone
    assert '已在自選' not in standalone
    assert '>Pro<' not in standalone
    assert 'pro-badge' not in standalone
    assert '個股籌碼清單UI.html' in server
    assert '@app.get("/chips/detail")' in server
    assert '個股籌碼彈窗UI.html' in server
    assert 'filename = "個股籌碼彈窗UI.html" if code' in server
    # 2026-09-01 起改讀 /api/stock/{code} 直接拿 chip 區塊，不再另外
    # race /api/chips/ + /api/watchpool + 一個不存在的 volume-history
    # route（那個 shim 本身就是舊 bug 的來源，見 chips_official SSOT 修復）。
    assert 'api/stock/' in modal
    assert 'role="dialog"' in modal
    assert '法人當日買賣超' in modal
    assert '近 5 日' in modal
    assert '近 20 日' in modal
    assert 'ch.source_date' in modal
    assert "position:fixed" in listing
    assert "api/watchpool" in listing
    assert "資金籌碼快覽" in listing
    assert "/chips/detail?code=" in listing
    assert 'class="app-bottom-nav"' not in standalone
    assert 'class="front-link"' in listing
    assert "大買" in listing
    assert "大賣" in listing
    assert '"inst_net_d_lots": inst_daily' in extras
    assert '"inst_net_5d_lots": inst_5d' in extras
    assert '"volume_history"' in extras
    assert '@app.get("/chips")' in server
    assert '@app.get("/api/chips/{code}")' in server
    assert '@app.get("/api/volume-history/{code}")' in server
    assert 'broker.daily_kbars' in server
    assert 'asof: str = None' in server
    # The modal no longer makes a second, separately-asof'd volume-history
    # request (that's what "chartAsOf" used to guard against staleness for);
    # it now reads volume_history bundled in the same /api/stock/ response
    # as source_date, so the two can no longer drift out of sync.
    assert 'p.volume_history' in modal


def test_watchpool_reuses_persisted_intraday_snapshot_before_daily_kbar(monkeypatch):
    saved_rows = [
        {"code": "1815", "price": 125.5, "change_rate": 7.73},
        {"code": "2330", "price": 980.0, "change_rate": -1.2},
    ]
    monkeypatch.setattr(extras, "_raw_rows", lambda: [])
    monkeypatch.setattr(
        extras.VIT,
        "_read_intraday_snapshot",
        lambda allow_prev_day=False: {"trade_date": "2026-08-31", "rows": saved_rows},
    )

    calls = []
    monkeypatch.setattr(extras.stock_card, "_bars", lambda *args, **kwargs: calls.append(args) or [])

    rows = extras._watchpool_rows_map()

    assert rows["1815"]["price"] == 125.5
    assert rows["1815"]["data_mode"] == "vps_persisted_intraday_snapshot"
    assert rows["1815"]["source_date"] == "2026-08-31"
    assert rows["2330"]["price"] == 980.0
    assert calls == []


def test_official_cache_rejects_incomplete_rolling_window(monkeypatch, tmp_path):
    cache = tmp_path / "chips_cache.json"
    original = {"date": "2026-08-31", "stocks": {"1815": {"days_used": 3}}}
    cache.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(chips_official, "CACHE_FILE", cache)
    monkeypatch.setattr(chips_official, "_trading_days_data", lambda max_days=20: [
        ("2026-08-28", {"1815": {"foreign": 1, "trust": 2, "dealer": 3,
                                  "dealer_self": 1, "dealer_hedge": 2, "total": 6}}),
    ])

    assert chips_official.build_cache(["1815"]) == 0
    assert json.loads(cache.read_text(encoding="utf-8")) == original


def test_official_cache_refresh_updates_detail_institutional_fields(monkeypatch, tmp_path):
    cache = tmp_path / "chips_cache.json"
    cache.write_text(json.dumps({"date": "2026-08-31", "stocks": {
        "1815": {"foreign_net_20d": 3},
        "detail:1815": {"foreign_net_20d": 3, "margin_balance": 99},
    }}), encoding="utf-8")
    monkeypatch.setattr(chips_official, "CACHE_FILE", cache)
    days = []
    for i in range(20):
        days.append((f"2026-08-{28 - i:02d}", {"1815": {
            "foreign": 10, "trust": 2, "dealer": 1,
            "dealer_self": 0, "dealer_hedge": 1, "total": 13,
        }}))
    monkeypatch.setattr(chips_official, "_trading_days_data", lambda max_days=20: days)

    assert chips_official.build_cache(["1815"]) == 1
    saved = json.loads(cache.read_text(encoding="utf-8"))["stocks"]
    assert saved["1815"]["inst_net_20d_lots"] == 260
    assert saved["detail:1815"]["inst_net_20d_lots"] == 260
    assert saved["detail:1815"]["foreign_net_20d"] == 200
    assert saved["detail:1815"]["margin_balance"] == 99


def test_official_cache_5d_and_20d_diverge_for_strong_buy_and_sell_regression(monkeypatch, tmp_path):
    """迴歸測試：2026-08-31 曾發生 5D 只吃到 3 個交易日、且與 20D 數字
    完全相同（detail 快取沒被新窗口覆蓋）。用一組大買(2455)+一組大賣(2327)、
    20 天皆非零且每日不同值的資料，鎖死「5D 必須只取最近 5 天、20D 必須
    取滿 20 天、兩者不可意外相等、自營自行/避險必須分開累加」。"""
    cache = tmp_path / "chips_cache.json"
    cache.write_text(json.dumps({"date": "2026-08-31", "stocks": {}}), encoding="utf-8")
    monkeypatch.setattr(chips_official, "CACHE_FILE", cache)

    days = []
    for i in range(20):
        # 新→舊：每天數字不同，才能檢查出「視窗切錯天數」或「5D=20D」這類 bug。
        days.append((f"2026-08-{31 - i:02d}", {
            "2455": {"foreign": 100 + i, "trust": 10 + i, "dealer": 5,
                      "dealer_self": 2, "dealer_hedge": 3, "total": 115 + i * 2},
            "2327": {"foreign": -(200 + i), "trust": -(20 + i), "dealer": -8,
                      "dealer_self": -3, "dealer_hedge": -5, "total": -(228 + i * 2)},
        }))
    monkeypatch.setattr(chips_official, "_trading_days_data", lambda max_days=20: days)

    assert chips_official.build_cache(["2455", "2327"]) == 2
    stocks = json.loads(cache.read_text(encoding="utf-8"))["stocks"]

    buy, sell = stocks["2455"], stocks["2327"]
    for rec in (buy, sell):
        assert rec["days_used"] == 20
        assert rec["foreign_net_5d"] != rec["foreign_net_20d"]
        assert rec["dealer_net_5d"] == rec["dealer_self_net_5d"] + rec["dealer_hedge_net_5d"]
        assert rec["dealer_net_20d"] == rec["dealer_self_net_20d"] + rec["dealer_hedge_net_20d"]

    # 5D 只加最近 5 天 (i=0..4)：foreign = sum(100+i for i in 0..4) = 510
    assert buy["foreign_net_5d"] == 510
    assert buy["foreign_net_20d"] == sum(100 + i for i in range(20))
    assert sell["foreign_net_5d"] == -sum(200 + i for i in range(5))
    assert sell["foreign_net_20d"] == -sum(200 + i for i in range(20))
    # 一買一賣方向不可混淆
    assert buy["foreign_net_5d"] > 0 and sell["foreign_net_5d"] < 0
