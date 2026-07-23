import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import analyst


class InstitutionalAggregationTest(unittest.TestCase):
    def test_negative_share_conversion_truncates_toward_zero(self):
        self.assertEqual(analyst._shares_to_lots(-8197500), -8197)
        self.assertEqual(analyst._shares_to_lots(3334500), 3334)

    def test_five_day_window_sums_each_institution_once(self):
        rows = []
        for day in range(1, 6):
            date = f"2026-07-{day:02d}"
            rows.extend([
                {"date": date, "name": "Foreign_Investor", "buy": 0, "sell": 1639400},
                {"date": date, "name": "Investment_Trust", "buy": 666900, "sell": 0},
                {"date": date, "name": "Dealer_self", "buy": 200, "sell": 0},
            ])
        original = analyst.dc.fetch_finmind_inst
        analyst.dc.fetch_finmind_inst = lambda code, days=10: rows
        try:
            result = analyst._inst_breakdown("6213")
        finally:
            analyst.dc.fetch_finmind_inst = original
        self.assertEqual(result["foreign_5d"], -8197)
        self.assertEqual(result["invest_5d"], 3334)
        self.assertEqual(result["dealer_5d"], 1)
        self.assertEqual(sum(result[k] for k in ("foreign_5d", "invest_5d", "dealer_5d")), -4862)


if __name__ == "__main__":
    unittest.main()
