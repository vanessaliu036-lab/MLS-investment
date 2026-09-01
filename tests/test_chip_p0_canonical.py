"""P0 chip/A-flow data-integrity regression tests."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(MODULE_DIR))

import chips  # noqa: E402
import money_health_api as mh  # noqa: E402
import vps_intraday_test as vit  # noqa: E402


def _row(date, name, buy, sell):
    return {"date": date, "name": name, "buy": buy, "sell": sell}


def test_finmind_three_institution_total_includes_dealers():
    rows = [
        _row("2026-08-28", "Foreign_Investor", 3_000_000, 1_000_000),
        _row("2026-08-28", "Investment_Trust", 1_500_000, 1_000_000),
        _row("2026-08-28", "Dealer_self", 100_000, 400_000),
        _row("2026-08-28", "Dealer_Hedging", 50_000, 250_000),
        _row("2026-08-31", "Foreign_Investor", 500_000, 1_500_000),
        _row("2026-08-31", "Investment_Trust", 800_000, 600_000),
        _row("2026-08-31", "Dealer_self", 400_000, 100_000),
        _row("2026-08-31", "Dealer_Hedging", 200_000, 100_000),
    ]

    got = chips.summarize_finmind_institutional(rows, inst_days=20)

    assert got["inst_net_20d_lots"] == 1600
    assert got["inst_net_5d_lots"] == 1600
    assert got["dealer_net_d"] == 400
    assert got["dealer_net_20d"] == -100
    assert got["source_date"] == "2026-08-31"


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

    monkeypatch.setattr(
        chips, "_finmind",
        lambda dataset, code, start_date:
            rows if dataset == "TaiwanStockInstitutionalInvestorsBuySell" else [],
    )

    got = chips.get_chips("3532")

    assert got["inst_net_20d_lots"] == 1300
    # No real holder-distribution source was supplied in this test.  The
    # institutional-lot value may never be relabelled as a holder percentage.
    assert got["big_holder_pct"] is None
    assert got["big_holder_trend"] is None


def test_popup_does_not_convert_multi_day_net_lots_using_current_price():
    html = (ROOT / "個股籌碼彈窗UI.html").read_text(encoding="utf-8")

    assert "Number(v)*1000*Number(price)/1e8" not in html
    assert "money(five,price)" not in html
    assert "money(twenty,price)" not in html
    assert "三大法人當日買賣超（張）" in html
    assert "三大法人近 5 日（張）" in html
    assert "三大法人近 20 日（張）" in html


def test_stock_card_exposes_source_specific_dates_and_identity():
    source = (MODULE_DIR / "stock_card.py").read_text(encoding="utf-8")
    assert '"chip_data_date": cd.get("source_date")' in source
    assert '"margin_source_date": cd.get("margin_source_date")' in source
    assert '"foreign_share_source_date": cd.get("foreign_share_source_date")' in source
    assert '"lending_source_date": cd.get("lending_source_date")' in source


def test_unavailable_aflow_cannot_become_actionable():
    raw = {
        "price": 100,
        "change_rate": 4.0,
        "total_volume": 10_000,
        "buy_volume": 9_000,
        "sell_volume": 1_000,
        "_aflow_unavailable": True,
    }
    got = vit._seven_factor_score(
        raw, ma20=90,
        chip={"inst_streak": 5, "inst_net_20d_lots": 8_000},
    )

    assert "主動買賣差" in got["score_missing"]
    assert got["group"] != "可操作"


def test_missing_net_active_hard_caps_nexora_to_watch():
    risk = {
        "ma_break": 0,
        "divergence": 0,
        "proxy": 0,
        "data_incomplete": 1,
        "net_active_missing": 1,
    }
    grade, capped, reason, hard = mh.grade_and_reason(
        95, "in_up", 1, risk, "attack", above_ma20=True
    )

    assert grade == "Watch"
    assert capped is True
    assert "DATA_INCOMPLETE" in reason
    assert "net_active" in reason


def test_chip_note_distinguishes_three_institution_total_from_foreign_streak():
    _score, _ok, note, _ev, _quality = mh.score_chip({
        "inst_net_20d_lots": 6631,
        "inst_streak": -5,
        "big_holder_trend": None,
    })

    assert "三大法人20日合計+6,631張" in note
    assert "外資連賣5日" in note
    assert "法人近月賣超48" not in note


def test_canonical_intraday_rows_expose_snapshot_identity_fields():
    source = (ROOT / "vps_intraday_test.py").read_text(encoding="utf-8")
    for field in ("snapshot_id", "snapshot_time", "source_table", "source_version"):
        assert f'"{field}"' in source


def test_watchpool_reuses_canonical_intraday_snapshot_path():
    source = (MODULE_DIR / "extras.py").read_text(encoding="utf-8")
    assert "VIT.intraday_test()" in source
