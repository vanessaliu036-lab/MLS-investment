import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from scoring import FAIL, NO_DATA, PASS, StockInput, compute_health_score


def make_stock(**changes):
    values = dict(
        code="T", name="測試", sector="測試族群", quadrant="流入↗漲",
        day_change_pct=2, active_buysell_diff=0.2, vol_ratio=1,
        legal_20d_net=8000, foreign_20d=5000, trust_20d=3000,
        legal_5d_net=2000, foreign_5d=1000, trust_5d=1000,
        legal_consec_days=2, margin_5d_chg=-300,
        close=105, ma20=100, above_ma20=True, bias_pct=5,
        foreign_turn_buy=PASS, margin_down=PASS, dahu_hold=NO_DATA,
        price_hold=PASS, vs_sector_pct=1, near_limit_up=False,
        volume_blowout=False, no_breakout=False, dahu_custody=NO_DATA,
    )
    values.update(changes)
    return StockInput(**values)


class ContinuousScoringTest(unittest.TestCase):
    def test_large_institutional_buying_scores_above_large_selling(self):
        buy = compute_health_score(make_stock(legal_20d_net=14189))
        sell = compute_health_score(make_stock(legal_20d_net=-6472, above_ma20=False, price_hold=FAIL))
        self.assertGreater(buy["score"], sell["score"])
        self.assertGreater(buy["score"] - sell["score"], 20)

    def test_no_data_is_not_a_pass(self):
        result = compute_health_score(make_stock(foreign_turn_buy=NO_DATA, dahu_hold=NO_DATA))
        self.assertEqual(result["detail"]["承接"]["外資轉買"], 0)
        self.assertEqual(result["detail"]["承接"]["大戶"], 0)
        self.assertIn("大戶集保無資料（不計分）", result["soft_risk"])

    def test_hard_risk_caps_grade_and_score(self):
        result = compute_health_score(make_stock(margin_5d_chg=3000, day_change_pct=8, near_limit_up=True))
        self.assertEqual(result["grade"], "Watch")
        self.assertLessEqual(result["score"], 60)


if __name__ == "__main__":
    unittest.main()
