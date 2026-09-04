"""Official institutional-data missing-value regression tests."""

import sys
from datetime import date
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1] / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(MODULE_DIR))

import chips_official  # noqa: E402


def test_missing_official_value_is_not_coerced_to_zero():
    row = ["2330"] + ["0"] * 23
    row[10] = ""
    row[13] = "1000"
    row[16] = "0"
    row[19] = "0"
    row[22] = "1000"
    row[-1] = "1000"

    monkey = lambda _url: {
        "tables": [{"data": [row]}],
    }
    original = chips_official._get_json
    chips_official._get_json = monkey
    try:
        assert chips_official._tpex_day(date(2026, 9, 3)) == {}
    finally:
        chips_official._get_json = original


def test_official_zero_value_remains_a_real_zero():
    assert chips_official._lots("0") == 0
    assert chips_official._lots("") is None
    assert chips_official._complete_lots("0", "0") == 0
    assert chips_official._complete_lots("0", "") is None
