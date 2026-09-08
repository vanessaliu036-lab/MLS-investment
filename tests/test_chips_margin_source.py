"""融資融券來源日回歸測試。

兩個實際事故：
1. 官方當日檔要收盤後才發布，舊碼只認 asof 當天，抓不到就靜默沿用上一次成功
   的舊餘額 —— 2026-09-08 盤中全池融資融券卡在 2026-09-04。
2. 上市融資融券抓錯表：TWT93U 是「信用額度總量管制餘額表」(前半融券限額、
   後半借券賣出)，沒有融資餘額；正確來源是 MI_MARGN 的融資融券彙總表。
"""

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1] / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(MODULE_DIR))   # chips.py 自己會 import config 等同層模組
# 直接從路徑載入並取獨立模組名：repo 裡有好幾支 chips.py，用 sys.path + import
# 會被其他測試先 import 的那一支蓋掉(既有 chips 測試就是這樣整批 fail)。
_SPEC = importlib.util.spec_from_file_location(
    "chips_stock_card_margin", MODULE_DIR / "chips.py")
chips = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(chips)


MARGN_ROWS = [["2330", "台積電", "715", "1,567", "12", "28,381", "27,517",
               "6,483,092", "1", "26", "5", "25", "45", "6,483,092", "0", " "]]
MARGN_ROWS += [[f"{9000 + i}", "假股", "0", "0", "0", "0", "0", "0",
                "0", "0", "0", "0", "0", "0", "0", " "] for i in range(150)]
TWT93U_ROWS = [["2330", "台積電", "25,000", "26,000", "1,000", "5,000",
                "45,000", "6,483,092,516", "15,994,514", "26,000", "51,000",
                "0", "15,969,514", "8,222,371", " "]]
TPEX_ROWS = [["4979", "華星光", "11,078", "2,250", "1,008", "6", "12,314",
              "187", "34.48", "35,705", "458", "9", "50", "0", "417", "8"]]


class _Resp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(published):
    """只有 ``published`` 那天有資料，其餘交易日回空表（官方尚未發布）。"""
    def _open(req, timeout=None):
        import urllib.parse
        url = urllib.parse.unquote(
            req.full_url if hasattr(req, "full_url") else str(req))
        has = published.replace("-", "") in url or published.replace("-", "/") in url
        if "MI_MARGN" in url:
            return _Resp({"stat": "OK", "tables": [
                {"title": "信用交易統計", "data": [["融資(交易單位)"]]},
                {"title": "融資融券彙總", "data": MARGN_ROWS if has else []},
            ]})
        if "TWT93U" in url:
            return _Resp({"stat": "OK", "data": TWT93U_ROWS if has else []})
        return _Resp({"tables": [{"data": TPEX_ROWS if has else []}]})
    return _open


def test_margin_snapshot_falls_back_to_previous_published_trading_day(monkeypatch):
    """當日檔還沒發布時要退到前一交易日，並標它自己的資料日。"""
    monkeypatch.setattr(chips.urllib.request, "urlopen", _fake_urlopen("2026-09-07"))
    monkeypatch.setattr(chips, "_official_margin_cache", {})

    snap = chips._official_margin_snapshot("2026-09-08")

    assert snap["2330"]["source_date"] == "2026-09-07"
    assert snap["4979"]["source_date"] == "2026-09-07"
    # 上市融資餘額來自 MI_MARGN(張)，不是 TWT93U 的融券限額。
    assert snap["2330"]["margin_balance"] == 27517
    assert snap["2330"]["margin_prev"] == 28381
    assert snap["2330"]["short_balance"] == 45
    # 借券賣出餘額仍走 TWT93U 的第二段(股→張)。
    assert snap["2330"]["sbl_balance"] == 15970
    assert snap["2330"]["sbl_source_date"] == "2026-09-07"
    # 上櫃走 TPEx，張為單位。
    assert snap["4979"]["margin_balance"] == 12314
    assert snap["4979"]["short_balance"] == 417


def test_margin_snapshot_does_not_cache_empty_result(monkeypatch):
    """整批抓不到時不得寫進快取，否則官方稍晚發布也不會再抓。"""
    monkeypatch.setattr(chips.urllib.request, "urlopen", _fake_urlopen("1990-01-01"))
    cache = {}
    monkeypatch.setattr(chips, "_official_margin_cache", cache)

    assert chips._official_margin_snapshot("2026-09-08") == {}
    assert cache == {}


def _freeze_hour(monkeypatch, hour):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, hour, 0, 0)
    monkeypatch.setattr(chips, "datetime", _DT)
    monkeypatch.setattr(chips, "_today_key", lambda: "2026-09-08")


def test_stale_margin_date_invalidates_detail_cache(monkeypatch):
    """法人更新到今天，不代表融資融券的舊日期也算新鮮。"""
    _freeze_hour(monkeypatch, 11)

    assert chips._daily_source_floor("2026-09-08") == "2026-09-07"
    assert not chips._daily_sources_fresh(
        {"margin_source_date": "2026-09-04",
         "lending_source_date": "2026-09-04"}, "2026-09-08")
    assert chips._daily_sources_fresh(
        {"margin_source_date": "2026-09-07",
         "lending_source_date": "2026-09-07"}, "2026-09-08")
    assert not chips._daily_sources_fresh(
        {"margin_source_date": None, "lending_source_date": None},
        "2026-09-08")


def test_after_close_requires_same_day_margin(monkeypatch):
    """收盤後當日檔已發布，還停在前一交易日就要重抓。"""
    _freeze_hour(monkeypatch, 20)

    assert chips._daily_source_floor("2026-09-08") == "2026-09-08"
    assert not chips._daily_sources_fresh(
        {"margin_source_date": "2026-09-07",
         "lending_source_date": "2026-09-07"}, "2026-09-08")


def test_lending_volume_date_does_not_speak_for_the_balance(monkeypatch, tmp_path):
    """借券成交量當天就有、賣出餘額要等官方收盤檔，兩者不得共用一個日期。"""
    cache = tmp_path / "chips_cache.json"
    cache.write_text('{"date":"","stocks":{}}', encoding="utf-8")
    monkeypatch.setattr(chips, "CACHE_FILE", str(cache))
    monkeypatch.setattr(chips, "_cache", {"date": "", "stocks": {}})
    monkeypatch.setattr(chips, "_save_disk", lambda: None)
    monkeypatch.setattr(chips, "_official_detail", lambda code, asof=None: {})
    monkeypatch.setattr(chips, "_official_margin_snapshot", lambda asof=None: {
        "3037": {"margin_prev": 35511, "margin_balance": 35134,
                 "short_prev": 602, "short_balance": 429,
                 "sbl_prev": 4426, "sbl_balance": 4144,
                 "source_date": "2026-09-07", "sbl_source_date": "2026-09-07"},
    })

    def _finmind(dataset, code, start, **kwargs):
        if dataset == "TaiwanStockSecuritiesLending":
            return [{"date": "2026-09-08", "volume": 356}]
        raise RuntimeError("FinMind 402")   # 免費額度用盡 → 走官方備援

    monkeypatch.setattr(chips, "_finmind", _finmind)

    r = chips.get_chips_detail("3037", asof="2026-09-08")

    assert r["lending_source_date"] == "2026-09-07"        # 餘額日
    assert r["lending_volume_source_date"] == "2026-09-08"  # 成交量日
    assert r["lending_balance"] == 4144
    assert r["margin_source_date"] == "2026-09-07"
    assert r["margin_balance"] == 35134


QFIIS_ROWS = {
    "2026-09-07": [["3037", "欣興", "TW0003037008", "1,641,291,596",
                    "975,396,070", "665,895,526", 59.42, 40.57,
                    "100.00", "100.00", "", "115/09/03"]],
    "2026-09-04": [["3037", "欣興", "TW0003037008", "1,641,291,596",
                    "976,381,000", "664,910,596", 59.48, 40.51,
                    "100.00", "100.00", "", "115/09/03"]],
}


def test_official_shareholding_falls_back_and_dates_itself(monkeypatch):
    """FinMind 402 時上市外資持股改吃 TWSE 官方，並標它自己的資料日。"""
    def _open(req, timeout=None):
        import re
        import urllib.parse
        url = urllib.parse.unquote(
            req.full_url if hasattr(req, "full_url") else str(req))
        ymd = re.search(r"date=(\d{8})", url)
        day = (f"{ymd.group(1)[:4]}-{ymd.group(1)[4:6]}-{ymd.group(1)[6:]}"
               if ymd else "")
        return _Resp({"stat": "OK", "data": QFIIS_ROWS.get(day, [])})

    monkeypatch.setattr(chips.urllib.request, "urlopen", _open)
    monkeypatch.setattr(chips, "_official_share_cache", {})

    snap = chips._official_shareholding_snapshot("2026-09-08")

    assert snap["3037"]["source_date"] == "2026-09-07"
    assert snap["3037"]["foreign_share_pct"] == 40.57
    assert snap["3037"]["foreign_share_remain_pct"] == 59.42


def test_stale_finmind_margin_falls_back_to_official(monkeypatch, tmp_path):
    """FinMind 抓得到，但只到更早的交易日 → 一樣要改吃官方。"""
    cache = tmp_path / "chips_cache.json"
    cache.write_text('{"date":"","stocks":{}}', encoding="utf-8")
    monkeypatch.setattr(chips, "CACHE_FILE", str(cache))
    monkeypatch.setattr(chips, "_cache", {"date": "", "stocks": {}})
    monkeypatch.setattr(chips, "_save_disk", lambda: None)
    monkeypatch.setattr(chips, "_official_detail", lambda code, asof=None: {})
    monkeypatch.setattr(chips, "_official_shareholding_snapshot", lambda asof=None: {})
    monkeypatch.setattr(chips, "_official_margin_snapshot", lambda asof=None: {
        "1815": {"margin_prev": 41788, "margin_balance": 43912,
                 "short_prev": 0, "short_balance": 0,
                 "sbl_prev": 14000, "sbl_balance": 14247,
                 "source_date": "2026-09-07", "sbl_source_date": "2026-09-07"},
    })
    _freeze_hour(monkeypatch, 11)   # floor = 2026-09-07

    def _finmind(dataset, code, start, **kwargs):
        if dataset == "TaiwanStockMarginPurchaseShortSale":
            return [{"date": "2026-09-04",
                     "MarginPurchaseTodayBalance": 41788,
                     "MarginPurchaseYesterdayBalance": 41000,
                     "ShortSaleTodayBalance": 0,
                     "ShortSaleYesterdayBalance": 0}]
        return []

    monkeypatch.setattr(chips, "_finmind", _finmind)

    r = chips.get_chips_detail("1815", asof="2026-09-08")

    assert r["margin_source_date"] == "2026-09-07"
    assert r["margin_balance"] == 43912
