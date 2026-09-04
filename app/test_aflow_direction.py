import unittest

from app import intraday_filter as F


class AflowDirectionTests(unittest.TestCase):
    def test_canonical_bid_side_is_buy_and_ask_side_is_sell(self):
        self.assertEqual(F.aflow_from_sides(1800, 1300), 500)
        self.assertEqual(F.aflow_from_sides(1300, 1800), -500)

    def test_ticktype_direction_reconciles_with_canonical_side_totals(self):
        official = F.aflow_from_sides(1800, 1300)
        tick = F.aflow_ticktype([(1, 700), (2, 200)])
        self.assertEqual(official, tick)
        self.assertFalse(F.aflow_reconcile(official, tick)["diverged"])

    def test_legacy_entry_keeps_8000_positional_contract_until_migrated(self):
        # Existing 8000 calls aflow_official(sell, buy); keep its value stable.
        self.assertEqual(F.aflow_official(1300, 1800), 500)


if __name__ == "__main__":
    unittest.main()
