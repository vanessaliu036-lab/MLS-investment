import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import intraday_note


class IntradayNoteSemanticsTests(unittest.TestCase):
    def test_positive_price_positive_aflow_is_price_flow_not_price_volume(self):
        text = intraday_note.build("", 1200, 2.5)
        self.assertIn("價流同步", text)
        self.assertNotIn("量價同步", text)

    def test_positive_price_negative_aflow_uses_aflow_language_not_institutional_sell(self):
        text = intraday_note.build("", -800, 2.5)
        self.assertIn("A-flow", text)
        self.assertNotIn("法人", text)


if __name__ == "__main__":
    unittest.main()
