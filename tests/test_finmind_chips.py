"""FinMind 外資資料解析：只用外資列判斷連買/連賣。"""
from pathlib import Path
import sys


MODULE_DIR = Path(__file__).resolve().parents[1] / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(MODULE_DIR))

import chips  # noqa: E402


def _row(date, name, buy, sell):
    return {"date": date, "name": name, "buy": buy, "sell": sell}


def test_finmind_summary_uses_foreign_rows_not_three_institution_total():
    rows = [
        _row("2026-08-22", "Foreign_Investor", 3000, 1000),
        _row("2026-08-22", "Investment_Trust", 0, 9000),
        _row("2026-08-25", "Foreign_Investor", 5000, 1000),
        _row("2026-08-25", "Investment_Trust", 0, 8000),
        _row("2026-08-26", "Foreign_Investor", 7000, 1000),
        _row("2026-08-26", "Investment_Trust", 0, 7000),
    ]
    result = chips.summarize_finmind_institutional(rows, inst_days=20)

    # 外資三天都是買超，即使投信三天都賣超，也仍應判斷外資連買 3 日。
    assert result["inst_streak"] == 3
    assert result["foreign_days"] == 3
    assert result["foreign_net_d"] == 6
    assert result["foreign_net_3d"] == 12
    assert result["source_date"] == "2026-08-26"


def test_finmind_summary_stops_streak_at_zero_or_direction_change():
    rows = [
        _row("2026-08-22", "Foreign_Investor", 1000, 2000),
        _row("2026-08-25", "Foreign_Investor", 1000, 1000),
        _row("2026-08-26", "Foreign_Investor", 2000, 1000),
    ]
    result = chips.summarize_finmind_institutional(rows)
    assert result["inst_streak"] == 1


def test_failed_none_cache_is_not_treated_as_completed_today(monkeypatch, tmp_path):
    cache = tmp_path / "chips_cache.json"
    cache.write_text(
        '{"date":"2026-08-27","stocks":{"2330":{"inst_streak":null}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(chips, "CACHE_FILE", str(cache))
    monkeypatch.setattr(chips, "_today_key", lambda: "2026-08-27")
    monkeypatch.setattr(chips, "_finmind", lambda *args: [
        _row("2026-08-26", "Foreign_Investor", 3000, 1000),
        _row("2026-08-26", "Investment_Trust", 0, 0),
    ])
    monkeypatch.setattr(chips, "_cache", {"date": "", "stocks": {}})

    result = chips.get_chips("2330")
    assert result["inst_streak"] == 1
    assert result["source_date"] == "2026-08-26"
