import unittest

from app import intraday_filter as F


class AflowDirectionTests(unittest.TestCase):
    def test_bid_side_is_buy_and_ask_side_is_sell_per_shioaji_contract(self):
        self.assertEqual(F.aflow_official(1800, 1300), 500)
        self.assertEqual(F.aflow_official(1300, 1800), -500)

    def test_ticktype_direction_reconciles_with_official_side_totals(self):
        official = F.aflow_official(1800, 1300)
        tick = F.aflow_ticktype([(1, 700), (2, 200)])
        self.assertEqual(official, tick)
        self.assertFalse(F.aflow_reconcile(official, tick)["diverged"])


if __name__ == "__main__":
    unittest.main()
