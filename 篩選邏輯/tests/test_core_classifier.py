import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SCREEN_DIR = BASE / "screen-src" if (BASE / "screen-src").exists() else BASE
sys.path.insert(0, str(SCREEN_DIR))

import layered_score as ls


def feature_input(**overrides):
    bar = {
        "open": 101.0,
        "high": 101.5,
        "low": 94.0,
        "close": 95.0,
        "ma5": 100.0,
        "ma20": 99.0,
        "ma60": 96.0,
        "volume": 240_000,
        "vol_ma20": 120_000,
    }
    bar.update(overrides.pop("bar", {}))
    inst = {
        "foreign_net": -1200,
        "trust_net": -300,
        "dealer_net": 50,
        "total_net": -1450,
        "consecutive_days": -4,
    }
    inst.update(overrides.pop("inst", {}))
    params = {
        "change_rate": -4.0,
        "previous_change_rate": -1.5,
        "previous_bar": {"close": 99.0, "volume": 100_000, "ma5": 100.5, "ma20": 99.5},
        "aflow_today": -8_000,
        "aflow_previous": -3_000,
        "prior_changes": [-1.5, 0.4, -0.2, 1.0, 0.3],
        "sector_rel": -2.5,
    }
    params.update(overrides)
    return ls.build_input("TEST", bar, inst, **params)


class FourGateTests(unittest.TestCase):
    def test_all_four_gates_are_required_for_structural_failure(self):
        result = ls.score_layered(feature_input())
        self.assertEqual(result["classification"], "❌ 結構失效")
        self.assertEqual(result["failure_gate_count"], 4)
        self.assertTrue(all(result["failure_gates"].values()))

    def test_one_to_three_gates_never_reject(self):
        cases = {
            "missing_active_flow": {"aflow_previous": None},
            "price_structure_intact": {"bar": {"close": 100.5}},
            "volume_price_not_weak": {"bar": {"volume": 80_000}},
            "rebound_not_failed": {"bar": {"high": 98.0}},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                result = ls.score_layered(feature_input(**overrides))
                self.assertNotEqual(result["classification"], "❌ 結構失效")
                self.assertLess(result["failure_gate_count"], 4)

    def test_institution_selling_but_price_resists_is_reversal_candidate(self):
        result = ls.score_layered(feature_input(
            bar={"open": 99.0, "high": 101.0, "low": 98.5, "close": 100.0,
                 "ma5": 99.5, "ma20": 99.0, "volume": 180_000},
            change_rate=1.0,
            aflow_today=2_000,
            aflow_previous=-3_000,
        ))
        self.assertEqual(result["classification"], "🔄 反轉候選")
        self.assertTrue(result["turn_signals"])

    def test_first_limit_up_keeps_a_potential_but_blocks_chasing(self):
        result = ls.score_layered(feature_input(
            bar={"open": 103.0, "high": 113.0, "low": 102.5, "close": 113.0,
                 "ma5": 104.0, "ma20": 99.0, "ma60": 92.0,
                 "volume": 260_000, "vol_ma20": 120_000},
            inst={"foreign_net": 100, "trust_net": 20, "total_net": 120,
                  "consecutive_days": 1},
            change_rate=9.71,
            previous_change_rate=1.0,
            prior_changes=[1.0, -0.5, 0.8, 1.2, -0.3, 0.4, 0.2, -1.0, 0.6, 0.1],
            aflow_today=16_000,
            aflow_previous=-1_000,
            sector_rel=1.5,
        ))
        self.assertEqual(result["potential_grade"], "A")
        self.assertEqual(result["trend_stage"], "🔥 Day 1 首次突破")
        self.assertEqual(result["entry_status"], "禁止追高")
        self.assertEqual(result["classification"], "⏳ 強勢但不追")

    def test_output_category_is_always_one_of_the_five_approved_states(self):
        allowed = {"🔥 A級啟動", "🔄 反轉候選", "👀 保留觀察", "⏳ 強勢但不追", "❌ 結構失效"}
        for data in [feature_input(), feature_input(aflow_previous=None), feature_input(bar={"close": 102.0})]:
            self.assertIn(ls.score_layered(data)["classification"], allowed)


if __name__ == "__main__":
    unittest.main()
