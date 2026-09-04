import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import navigation
import line_b_layers as layers


class BuyPointMonitorSemanticsTests(unittest.TestCase):
    def test_navigation_distinguishes_c1c2_monitor_from_full_pool_layers(self):
        labels = {key: label for key, label, _href, _badge in navigation.NAV_ITEMS}
        self.assertEqual(labels["line-b"], "買點監控｜C1+C2")
        self.assertEqual(labels["layers"], "七層交易狀態｜全池")

    def test_untriggered_stock_does_not_report_key_price_as_current_failure(self):
        trig = {"verdict": "NO", "hold_slots": 0, "above_vwap": None}
        vol = {"verdict": "THIN", "vol_accel": None}
        acc = {"verdict": "N/A"}
        ext = {"verdict": "NORMAL"}
        flow = {"verdict": "NEGATIVE", "flow_state": "翻空", "chip_blocked": False}

        result = layers._trade_judgment(
            trig, vol, acc, ext, flow,
            state="WATCH", distance_pct=-3.73, structure_ok=True,
        )
        self.assertEqual(result["failure_alerts"], ["A-flow 翻負"])
        self.assertNotIn("跌破關鍵價", result["failure_alerts"])
        self.assertIn("跌破關鍵價", result["failure_conditions"])

    def test_key_price_failure_requires_prior_trigger_history(self):
        trig = {"verdict": "NO", "hold_slots": 2, "above_vwap": None}
        vol = {"verdict": "THIN", "vol_accel": None}
        acc = {"verdict": "N/A"}
        ext = {"verdict": "NORMAL"}
        flow = {"verdict": "POSITIVE", "flow_state": "持續", "chip_blocked": False}

        result = layers._trade_judgment(
            trig, vol, acc, ext, flow,
            state="WATCH", distance_pct=-0.5, structure_ok=True,
        )
        self.assertIn("跌破關鍵價", result["failure_alerts"])


if __name__ == "__main__":
    unittest.main()
