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

    def test_high_chase_risk_sets_entry_status_to_no_chase(self):
        result = ls.score_layered(feature_input(
            bar={"open": 100.0, "high": 111.0, "low": 99.0, "close": 109.0,
                 "ma5": 103.0, "ma20": 99.0, "volume": 400_000, "vol_ma20": 100_000},
            change_rate=8.0,
            prior_changes=[6.0, 5.5],
            aflow_today=10_000,
            aflow_previous=5_000,
        ))
        self.assertGreaterEqual(result["chase_risk"], ls.CHASE_RISK_MAX)
        self.assertEqual(result["classification"], ls.TIER_NO_CHASE)
        self.assertEqual(result["entry_status"], "禁止追高")


class PotentialTierTests(unittest.TestCase):
    """A 級細分(2026-08-19):同樣 potential_grade='A',T+1 性質不同——
    首次突破(A1)該排在延伸段(A3)前面。附加欄位,不改 potential_grade 既有語意。"""

    def test_day1_breakout_is_a1_highest_priority(self):
        result = ls.score_layered(feature_input(
            bar={"open": 103.0, "high": 113.0, "low": 102.5, "close": 113.0,
                 "ma5": 104.0, "ma20": 99.0, "ma60": 92.0,
                 "volume": 260_000, "vol_ma20": 120_000},
            inst={"foreign_net": 100, "trust_net": 20, "total_net": 120,
                  "consecutive_days": 1},
            change_rate=9.71,
            previous_change_rate=1.0,
            prior_changes=[1.0, -0.5, 0.8, 1.2, -0.3, 0.4, 0.2, -1.0, 0.6, 0.1],
            aflow_today=16_000, aflow_previous=-1_000, sector_rel=1.5,
        ))
        self.assertEqual(result["potential_grade"], "A")
        self.assertEqual(result["potential_tier"], "A1")
        self.assertEqual(result["potential_priority"], 0)

    def test_extended_day4_plus_is_a3_lower_priority_than_a1(self):
        # 連漲多日已進 Day4+ 高乖離階段,即使 continuation 仍高,T+1 性質跟新鮮突破不同。
        result = ls.score_layered(feature_input(
            bar={"open": 118.0, "high": 120.0, "low": 117.0, "close": 119.0,
                 "ma5": 108.0, "ma20": 99.0, "ma60": 92.0,
                 "volume": 200_000, "vol_ma20": 120_000},
            inst={"foreign_net": 800, "trust_net": 400, "total_net": 1200,
                  "consecutive_days": 3},
            change_rate=5.5,
            previous_change_rate=6.0,
            prior_changes=[6.0, 5.8, 6.2, 0.4, -0.2, 1.0, 0.3],
            aflow_today=5_000, aflow_previous=4_000, sector_rel=2.0,
            inst_3d=500, inst_5d=800,
        ))
        self.assertEqual(result["trend_stage"], "⏳ Day 4+ 高乖離／等待整理")
        if result["continuation"] >= ls.CONT_CORE_MIN:
            self.assertEqual(result["potential_grade"], "A")
            self.assertEqual(result["potential_tier"], "A3")
            self.assertGreater(result["potential_priority"], 0)

    def test_not_strong_enough_is_grade_b_priority_lowest(self):
        result = ls.score_layered(feature_input())
        self.assertEqual(result["potential_grade"], "B")
        self.assertEqual(result["potential_tier"], "B")
        self.assertEqual(result["potential_priority"], 3)

    def test_potential_tier_is_additive_and_equivalent_to_old_grade_formula(self):
        """potential_tier != 'B' 必須恰好等價於 potential_grade == 'A'(聯集不多不少),
        確保這是純附加欄位,沒有偷改既有 A/B 判定。"""
        cases = [
            feature_input(),
            feature_input(aflow_previous=None),
            feature_input(bar={"close": 102.0}),
            feature_input(change_rate=1.0, bar={"close": 100.0, "ma5": 99.5, "ma20": 98.0}),
        ]
        for data in cases:
            result = ls.score_layered(data)
            with self.subTest(trend_stage=result["trend_stage"], cont=result["continuation"]):
                self.assertEqual(result["potential_grade"] == "A",
                                 result["potential_tier"] != "B")


class FourDimensionTests(unittest.TestCase):
    """四維度拆分(2026-08-19):structural/momentum/entry/verification 各自獨立,
    同一檔可以 structural_status=OK 但 entry_status=禁止追高,不等於「淘汰」。"""

    def test_structural_status_ok_when_zero_gates_fail(self):
        result = ls.score_layered(feature_input(
            bar={"open": 99.0, "high": 101.0, "low": 98.5, "close": 100.5,
                 "ma5": 99.0, "ma20": 98.0, "volume": 90_000},
            change_rate=1.0, aflow_today=1_000, aflow_previous=1_000,
        ))
        self.assertEqual(result["failure_gate_count"], 0)
        self.assertEqual(result["structural_status"], ls.STRUCT_OK)

    def test_structural_status_at_risk_when_one_to_three_gates_fail(self):
        result = ls.score_layered(feature_input(bar={"close": 100.5}))
        self.assertIn(result["failure_gate_count"], (1, 2, 3))
        self.assertEqual(result["structural_status"], ls.STRUCT_AT_RISK)

    def test_structural_status_failed_only_when_all_four_gates_fail(self):
        result = ls.score_layered(feature_input())
        self.assertEqual(result["failure_gate_count"], 4)
        self.assertEqual(result["structural_status"], ls.STRUCT_FAILED)
        self.assertEqual(result["classification"], ls.TIER_REJECTED)

    def test_momentum_status_mirrors_trend_stage(self):
        result = ls.score_layered(feature_input())
        self.assertEqual(result["momentum_status"], result["trend_stage"])

    def test_high_chase_risk_never_produces_structural_failed(self):
        """Cut 6 鎖死:chase_risk 只能決定 entry_status(禁追/等待觸發),
        不能讓 structural_status 變成 FAILED——追價風險高不是結構失效。"""
        result = ls.score_layered(feature_input(
            bar={"open": 100.0, "high": 111.0, "low": 99.0, "close": 109.0,
                 "ma5": 103.0, "ma20": 99.0, "volume": 400_000, "vol_ma20": 100_000},
            change_rate=8.0, prior_changes=[6.0, 5.5],
            aflow_today=10_000, aflow_previous=5_000,
        ))
        self.assertGreaterEqual(result["chase_risk"], ls.CHASE_RISK_MAX)
        self.assertEqual(result["entry_status"], "禁止追高")
        self.assertNotEqual(result["structural_status"], ls.STRUCT_FAILED)
        self.assertNotEqual(result["classification"], ls.TIER_REJECTED)

    def test_classify_never_rejects_on_chase_risk_alone_across_full_range(self):
        """classify() 的淘汰分支只吃 fails 長度;chase/chase_block 無論多極端,
        只要 fails < STRUCT_FAIL_MIN 就不可能回傳 TIER_REJECTED。"""
        for chase in (0, 25, 50, 70, 82, 100):
            for chase_block in (False, True):
                for fails in ([], ["價格結構破壞"], ["價格結構破壞", "量價轉弱", "反彈失敗"]):
                    tier = ls.classify(50.0, chase, fails, chase_block=chase_block)
                    with self.subTest(chase=chase, chase_block=chase_block, fails=len(fails)):
                        self.assertNotEqual(tier, ls.TIER_REJECTED)


if __name__ == "__main__":
    unittest.main()
