import unittest

import pullback_discovery as pd


def _slots(prices, volumes, flows):
    return [{"price": p, "volume": v, "net_active": f}
            for p, v, f in zip(prices, volumes, flows)]


class PullbackDiscoveryTests(unittest.TestCase):
    EV = {"code": "9999", "limitup_date": "2026-08-01", "limitup_close": 100.0,
          "d1_date": "2026-08-02", "d1_close": None, "d2_date": None, "d2_close": None}

    def test_no_pullback_stays_impulse(self):
        prices = [110 + i for i in range(12)]
        rec = pd.compute_case(self.EV, _slots(prices, [1000]*12, [10]*12))
        self.assertEqual(rec["classification"], "IMPULSE")
        self.assertNotIn("pullback_depth", rec)

    def test_pullback_without_reclaim_stays_pullback(self):
        prices = [110, 111, 112, 108, 107, 106, 105, 104, 103, 102, 101, 100]
        flows = [10, 11, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12]  # net_active 凍結不動,不達reclaim
        rec = pd.compute_case(self.EV, _slots(prices, [1000]*12, flows))
        self.assertEqual(rec["classification"], "PULLBACK")
        self.assertIn("pullback_depth", rec)
        self.assertNotIn("entry_price", rec)

    def test_reclaim_sets_entry_anchor_not_trough(self):
        # peak=112(idx2), 回落到 105(idx6), 再彈回並帶量帶flow → RECLAIMED
        prices = [110, 111, 112, 109, 107, 106, 105, 106.5, 108, 109, 110, 111]
        flows =  [10,  11,  12,  12,  12,  12,  12,  13,   14,  15,  16,  17]
        rec = pd.compute_case(self.EV, _slots(prices, [1000]*12, flows))
        self.assertEqual(rec["classification"], "RECLAIMED")
        self.assertEqual(rec["peak_idx"], 2)
        self.assertEqual(rec["trough_idx"], 6)
        self.assertEqual(rec["reclaim_idx"], 7)
        self.assertEqual(rec["entry_price"], prices[7])  # 錨在reclaim格,不是trough
        self.assertAlmostEqual(rec["net_h15m"], prices[10]/prices[7]-1)

    def test_support_hold_uses_prior_limitup_close(self):
        prices = [110, 111, 112, 109, 107, 106, 99, 100.5, 102, 103, 104, 105]
        flows =  [10,  11,  12,  12,  12,  12,  12, 13,    14,  15,  16,  17]
        rec = pd.compute_case(self.EV, _slots(prices, [1000]*12, flows))
        self.assertFalse(rec["support_hold"])  # trough(99) < limitup_close(100)

    def test_volume_contraction_uses_per_minute_rate_not_cumulative_level(self):
        # 累積量:拉抬段(0-2)加速累積,回撤段(3-7)幾乎不動 → 真量縮
        cvol = [0, 400, 1000, 1050, 1080, 1100, 1120, 1400, 1800, 2200, 2600, 3000]
        prices = [110, 111, 112, 109, 107, 106, 105, 106.5, 108, 109, 110, 111]
        flows =  [10,  11,  12,  12,  12,  12,  12,  13,    14,  15,  16,  17]
        rec = pd.compute_case(self.EV, _slots(prices, cvol, flows))
        self.assertEqual(rec["classification"], "RECLAIMED")
        self.assertIsNotNone(rec["volume_contraction"])
        self.assertLess(rec["volume_contraction"], 1.0)  # 回撤段速率 < 拉抬段速率

    def test_insufficient_slots_returns_none(self):
        rec = pd.compute_case(self.EV, _slots([1, 2, 3], [1, 1, 1], [1, 1, 1]))
        self.assertIsNone(rec)

    def test_missing_price_returns_none(self):
        slots = _slots([1]*12, [1]*12, [1]*12)
        slots[5]["price"] = None
        self.assertIsNone(pd.compute_case(self.EV, slots))


if __name__ == "__main__":
    unittest.main()
