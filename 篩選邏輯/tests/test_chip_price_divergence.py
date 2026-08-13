import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SCREEN_DIR = BASE / "screen-src" if (BASE / "screen-src").exists() else BASE
sys.path.insert(0, str(SCREEN_DIR))

import chip_price_divergence as cpd


def inst(values, streak=None):
    rows = []
    for index, value in enumerate(values):
        rows.append({"data_date": f"2026-08-{13-index:02d}", "total_net": value,
                     "foreign_net": value, "consecutive_days": streak if index == 0 else None})
    return rows


def bars(closes, *, lows=None, highs=None, volumes=None, ma20=90):
    lows = lows or [value - 1 for value in closes]
    highs = highs or [value + 1 for value in closes]
    volumes = volumes or [1_000_000] * len(closes)
    rows = []
    for index, close in enumerate(closes):
        low, high = lows[index], highs[index]
        rows.append({"data_date": f"2026-08-{13-index:02d}", "open": close,
                     "high": high, "low": low, "close": close,
                     "volume": volumes[index], "vol_ma20": 1_000_000,
                     "ma20": ma20})
    return rows


class ChipPriceDivergenceTests(unittest.TestCase):
    def test_a_sell_absorption(self):
        result = cpd.scan(
            inst([-800, -1000, -700, 200, -300], streak=-3),
            bars([100, 100.5, 100.2, 99.8, 99.5],
                 lows=[99, 98.5, 98.0, 97.5, 97.0]), [])
        self.assertEqual(result["divergence_type"], "sell_absorption")
        self.assertEqual(result["divergence_label"], "🟢 抗賣壓")
        self.assertEqual(result["divergence_action"], "prioritize")

    def test_b_washout_with_ownership_pending(self):
        result = cpd.scan(
            inst([300, 600, 700, 800, 900], streak=5),
            bars([97, 100, 99, 98, 97.5], lows=[96, 95, 94, 93, 92], ma20=95), [])
        self.assertEqual(result["divergence_type"], "washout")
        self.assertEqual(result["divergence_label"], "🟢 洗盤換手")
        self.assertIn("法人持股比例", result["divergence_pending"])
        self.assertEqual(result["divergence_action"], "pullback_watch")

    def test_c_chip_reversal_is_highest_priority(self):
        result = cpd.scan(
            inst([3000, -500, -700, -900, -600, -800], streak=1),
            bars([106, 100, 99, 98, 97, 96],
                 lows=[100, 97, 96, 95, 94, 93],
                 highs=[107, 102, 101, 100, 99, 98],
                 volumes=[1_600_000, 900_000, 900_000, 900_000, 900_000, 900_000]), [])
        self.assertEqual(result["divergence_type"], "chip_reversal")
        self.assertEqual(result["divergence_grade"], "S")
        self.assertEqual(result["divergence_priority"], "highest")
        self.assertEqual(result["divergence_action"], "upgrade_a")

    def test_d_buying_stall_blocks_chasing(self):
        result = cpd.scan(
            inst([30_000, 25_000, 22_500, 20_000, 17_500], streak=5),
            bars([100, 100.5, 100.2, 100.1, 100],
                 highs=[102, 103, 102.5, 102, 101.5],
                 volumes=[1_000_000] * 5), [])
        self.assertEqual(result["divergence_type"], "buying_stall")
        self.assertEqual(result["divergence_label"], "🟡 買盤鈍化")
        self.assertEqual(result["divergence_action"], "no_chase")

    def test_e_double_weak_is_evidence_not_a_rejection_gate(self):
        result = cpd.scan(
            inst([-1000, -900, -800, -700, -600], streak=-4),
            bars([90, 94, 95, 96, 97], lows=[89, 93, 94, 95, 96]), [])
        self.assertEqual(result["divergence_type"], "double_weak")
        self.assertEqual(result["divergence_label"], "🔴 籌碼價格雙殺")
        self.assertEqual(result["divergence_action"], "downgrade")
        self.assertFalse(result["can_reject"])

    def test_missing_history_returns_none_without_guessing(self):
        result = cpd.scan(inst([100, -100]), bars([101, 100]), [])
        self.assertEqual(result["divergence_type"], "none")
        self.assertIn("近5日法人", result["divergence_pending"])
        self.assertIn("近5日價格", result["divergence_pending"])

    def test_types_are_mutually_exclusive(self):
        result = cpd.scan(
            inst([3000, -500, -700, -900, -600, -800], streak=1),
            bars([106, 100, 99, 98, 97, 96],
                 lows=[100, 97, 96, 95, 94, 93],
                 highs=[107, 102, 101, 100, 99, 98],
                 volumes=[1_600_000, 900_000, 900_000, 900_000, 900_000, 900_000]), [])
        self.assertIsInstance(result["divergence_type"], str)
        self.assertNotIsInstance(result["divergence_type"], list)
        self.assertEqual(len(result["matched_types"]), 1)


if __name__ == "__main__":
    unittest.main()
