import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SCREEN_DIR = BASE / "screen-src" if (BASE / "screen-src").exists() else BASE
sys.path.insert(0, str(SCREEN_DIR))

import tw_price_limit as tw


class TaiwanPriceLimitTests(unittest.TestCase):
    def test_limit_prices_follow_market_tick_rounding(self):
        cases = [(312, 343), (933, 1025), (103, 113), (602, 662), (288, 316.5)]
        for reference, expected in cases:
            with self.subTest(reference=reference):
                self.assertEqual(tw.limit_up_price(reference), expected)

    def test_true_limit_rows_are_distinguished_from_nine_percent_gainers(self):
        cases = [
            (343, 9.93, True),
            (1025, 9.86, True),
            (113, 9.70, True),
            (662, 9.96, True),
            (316.5, 9.89, True),
            (161, 9.15, False),
            (747, 9.05, False),
        ]
        for price, change, expected in cases:
            with self.subTest(price=price):
                self.assertEqual(tw.is_limit_up(price, change_rate=change), expected)


if __name__ == "__main__":
    unittest.main()
