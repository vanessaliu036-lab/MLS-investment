import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import funnel


def stock(**overrides):
    row = {
        "code": "TEST",
        "quad": "in_up",
        "score": 80,
        "inst_net_20d": 1000,
        "inst_5d_net": 20,
        "trust_5d_net": 0,
        "above_ma20": True,
        "stars": 4,
        "vs_sector": -0.57,
        "margin_5d_chg": -100,
        "chg": 0.02,
        "near_limit": False,
        "grade": "Ready",
    }
    row.update(overrides)
    return row


class FunnelRulesTest(unittest.TestCase):
    def test_l2_uses_20_day_net_and_five_day_continuity(self):
        self.assertEqual(funnel.gate2_chip_technical(stock()), funnel.PASS)
        self.assertEqual(funnel.gate2_chip_technical(stock(above_ma20=1)), funnel.PASS)
        self.assertEqual(
            funnel.gate2_chip_technical(stock(inst_5d_net=-10, trust_5d_net=30)), funnel.PASS
        )
        self.assertEqual(
            funnel.gate2_chip_technical(stock(inst_5d_net=-10, trust_5d_net=0)), funnel.FAIL
        )

    def test_sector_underperformance_does_not_fail_l2(self):
        self.assertEqual(funnel.gate2_chip_technical(stock(vs_sector=-5.22)), funnel.PASS)

    def test_l3_margin_surge_caps_ready_but_margin_drop_is_bonus(self):
        risky = stock(margin_5d_chg=600, chg=8, near_limit=True)
        funnel.apply_l3_margin_rule(risky)
        self.assertEqual(risky["grade"], "Watch")
        washed = stock(margin_5d_chg=-561, score=70)
        funnel.apply_l3_margin_rule(washed)
        self.assertEqual(washed["score"], 75)

    def test_livermore_is_not_an_l1_to_l3_gate(self):
        result = funnel.run_funnel([stock(livermore={"state": "下降趨勢"})])
        self.assertEqual(result["passed_codes"], ["TEST"])


if __name__ == "__main__":
    unittest.main()
