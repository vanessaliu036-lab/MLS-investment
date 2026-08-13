import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SCREEN_DIR = BASE / "screen-src" if (BASE / "screen-src").exists() else BASE
sys.path.insert(0, str(SCREEN_DIR))

import layered_score as ls
import recovery_scan as rs


def rejected_features(**overrides):
    data = {
        "close": 99.5, "open": 100.0, "high": 101.0, "low": 98.5,
        "ma5": 100.0, "ma20": 101.0, "change_rate": -0.5,
        "total_net": -1000.0, "aflow_today": -500.0,
        "aflow_previous": -1000.0, "previous_low": 98.0,
        "sector_flow_turn": True,
    }
    data.update(overrides)
    return data


class RecoveryScanTests(unittest.TestCase):
    def test_only_rejected_stocks_are_scanned(self):
        result = rs.scan({"classification": ls.TIER_CANDIDATE}, rejected_features())
        self.assertFalse(result["eligible"])
        self.assertFalse(result["in_recovery_pool"])

    def test_recovery_never_changes_the_original_five_state_classification(self):
        classified = {"classification": ls.TIER_REJECTED}
        result = rs.scan(classified, rejected_features(
            close=101.5, low=98.5, ma5=100.0,
            aflow_today=500.0, aflow_previous=-1000.0,
        ))
        self.assertEqual(classified["classification"], ls.TIER_REJECTED)
        self.assertNotIn("A級", result["status"])

    def test_score_weights_and_high_priority_threshold(self):
        result = rs.scan({"classification": ls.TIER_REJECTED}, rejected_features())
        self.assertEqual(result["score"], 75)
        self.assertEqual(result["status"], "🔄 高優先救援")
        self.assertTrue(result["in_recovery_pool"])

    def test_late_recovery_scores_a_strong_rebound_that_still_closes_below_ma(self):
        result = rs.scan({"classification": ls.TIER_REJECTED}, rejected_features())
        self.assertIn("尾盤明顯拉回 +20", result["signals"])
        self.assertLess(rejected_features()["close"], rejected_features()["ma5"])

    def test_score_40_to_59_is_rejected_watch(self):
        result = rs.scan({"classification": ls.TIER_REJECTED}, rejected_features(
            close=99.0, aflow_previous=None, previous_low=100.0,
        ))
        self.assertEqual(result["score"], 40)
        self.assertEqual(result["status"], "👀 淘汰觀察")

    def test_sector_turn_requires_broad_positive_flow_after_non_positive_day(self):
        turns = rs.sector_flow_turns(
            ["A", "B", "C"], {"A": "S", "B": "S", "C": "S"},
            {"A": 100, "B": 50, "C": -10}, {"A": -50, "B": -20, "C": 0},
        )
        self.assertTrue(turns["A"])

    def test_t1_trigger_requires_all_three_confirmation_conditions(self):
        result = rs.evaluate_t1_trigger(price=105, aflow=100,
                                        ma5=101, rejected_high=104)
        self.assertTrue(result["triggered"])
        self.assertEqual(result["classification"], ls.TIER_REVERSAL)
        self.assertFalse(rs.evaluate_t1_trigger(
            price=103, aflow=100, ma5=101, rejected_high=104)["triggered"])

    def test_outflow_exhaustion_requires_at_least_thirty_percent_contraction(self):
        rejected = {"classification": ls.TIER_REJECTED}
        contracted = rs.scan(rejected, rejected_features(
            aflow_today=-700, aflow_previous=-1000,
            sector_flow_turn=False, previous_low=100.0,
        ))
        barely_smaller = rs.scan(rejected, rejected_features(
            aflow_today=-900, aflow_previous=-1000,
            sector_flow_turn=False, previous_low=100.0,
        ))
        self.assertIn("主動賣超縮小 +5", contracted["signals"])
        self.assertNotIn("主動賣超縮小 +5", barely_smaller["signals"])


if __name__ == "__main__":
    unittest.main()
