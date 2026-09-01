"""P0 chip-data integrity regression tests.

These tests intentionally fail against the pre-fix implementation.  The chip
layer must fail closed rather than relabel quantities or manufacture money/%
values from unrelated fields.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(MODULE_DIR))

import chips  # noqa: E402


def _row(date, name, buy, sell):
    return {"date": date, "name": name, "buy": buy, "sell": sell}


def test_finmind_three_institution_total_includes_dealers():
    rows = [
        _row("2026-08-28", "Foreign_Investor", 3_000_000, 1_000_000),  # +2,000 lots
        _row("2026-08-28", "Investment_Trust", 1_500_000, 1_000_000),  # +500
        _row("2026-08-28", "Dealer_self", 100_000, 400_000),            # -300
        _row("2026-08-28", "Dealer_Hedging", 50_000, 250_000),         # -200
        _row("2026-08-31", "Foreign_Investor", 500_000, 1_500_000),    # -1,000
        _row("2026-08-31", "Investment_Trust", 800_000, 600_000),      # +200
        _row("2026-08-31", "Dealer_self", 400_000, 100_000),            # +300
        _row("2026-08-31", "Dealer_Hedging", 200_000, 100_000),         # +100
    ]

    got = chips.summarize_finmind_institutional(rows, inst_days=20)

    # Day totals: +2,000 and -400 => +1,600 lots.  Dealer rows are part of
    # 三大法人 and may never be omitted from an institutional total.
    assert got["inst_net_20d_lots"] == 1600
    assert got["inst_net_5d_lots"] == 1600
    assert got["dealer_net_d"] == 400
    assert got["dealer_net_20d"] == -100


def test_get_chips_never_uses_institutional_lots_as_holder_percent(monkeypatch, tmp_path):
    cache = tmp_path / "chips_cache.json"
    cache.write_text('{"date":"","stocks":{}}', encoding="utf-8")
    monkeypatch.setattr(chips, "CACHE_FILE", str(cache))
    monkeypatch.setattr(chips, "_cache", {"date": "", "stocks": {}})
    monkeypatch.setattr(chips, "_today_key", lambda: "2026-09-01")
    monkeypatch.setattr(chips, "_save_disk", lambda: None)

    rows = [
        _row("2026-08-31", "Foreign_Investor", 2_000_000, 1_000_000),
        _row("2026-08-31", "Investment_Trust", 1_000_000, 800_000),
        _row("2026-08-31", "Dealer_self", 200_000, 100_000),
    ]

    def fake_finmind(dataset, code, start_date):
        if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
            return rows
        return []

    monkeypatch.setattr(chips, "_finmind", fake_finmind)

    got = chips.get_chips("3532")

    assert got["inst_net_20d_lots"] == 1300
    assert got["big_holder_pct"] is None
    assert got["big_holder_trend"] is None


def test_popup_does_not_convert_multi_day_net_lots_using_current_price():
    html = (ROOT / "個股籌碼彈窗UI.html").read_text(encoding="utf-8")

    # net lots × today's price is not a historical institutional money flow.
    assert "Number(v)*1000*Number(price)/1e8" not in html
    assert "money(five,price)" not in html
    assert "money(twenty,price)" not in html

    # Canonical UI must state the metric and unit explicitly.
    assert "三大法人當日買賣超（張）" in html
    assert "三大法人近 5 日（張）" in html
    assert "三大法人近 20 日（張）" in html


def test_stock_card_exposes_chip_data_date_alias():
    source = (MODULE_DIR / "stock_card.py").read_text(encoding="utf-8")
    assert '"chip_data_date": cd.get("source_date")' in source
