import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SCREEN_DIR = BASE / "screen-src" if (BASE / "screen-src").exists() else BASE
sys.path.insert(0, str(SCREEN_DIR))

import reject_verify


class RejectKpiTests(unittest.TestCase):
    def test_five_percent_false_negative_ignores_flow_and_relative_strength(self):
        row = reject_verify.judge_row(
            100, 104, 105, 101, 103,
            net_active=-9999, rel_strength=-4.0,
        )
        self.assertTrue(row["fnr_5"])
        self.assertFalse(row["fnr_9"])
        self.assertEqual(row["verdict"], "誤刪")

    def test_nine_percent_false_negative_counts_even_when_gap_is_large(self):
        row = reject_verify.judge_row(
            100, 106, 109, 105, 108,
            net_active=None, rel_strength=None,
        )
        self.assertTrue(row["fnr_5"])
        self.assertTrue(row["fnr_9"])
        self.assertEqual(row["verdict"], "嚴重誤刪")

    def test_below_five_percent_is_not_false_negative(self):
        row = reject_verify.judge_row(100, 100, 104.99, 98, 102)
        self.assertFalse(row["fnr_5"])
        self.assertFalse(row["fnr_9"])
        self.assertEqual(row["verdict"], "排對")

    def test_missing_price_data_is_not_counted_as_false_negative(self):
        row = reject_verify.judge_row(100, None, None, None, None)
        self.assertFalse(row["fnr_5"])
        self.assertFalse(row["fnr_9"])
        self.assertEqual(row["verdict"], "資料不足")

    def test_recovery_pool_kpis_use_only_predeclared_recovery_members(self):
        stats = reject_verify.recovery_kpis([
            {"recovery_pool": 1, "t1_high_ret": 6.0},
            {"recovery_pool": 1, "t1_high_ret": 10.0},
            {"recovery_pool": 0, "t1_high_ret": 12.0},
            {"recovery_pool": 1, "t1_high_ret": None},
        ])
        self.assertEqual(stats["denom"], 2)
        self.assertEqual(stats["hit_5_rate"], 100.0)
        self.assertEqual(stats["hit_9_rate"], 50.0)


if __name__ == "__main__":
    unittest.main()
