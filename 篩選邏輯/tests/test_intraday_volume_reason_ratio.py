import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import decision_view as dv
import layered_score as ls


class IntradayVolumeReasonRatioTests(unittest.TestCase):
    def test_reason_tag_uses_canonical_intraday_ratio_not_lot_share_division(self):
        classified = {"classification": ls.TIER_CANDIDATE, "potential_grade": "B",
                      "trend_stage": "未啟動"}
        market = {
            "close": 101.0, "current_price": 101.0, "high": 101.0, "low": 100.0,
            "prior_high": 105.0, "ma5": 99.0, "ma20": 98.0,
            "change_rate": 1.5, "aflow_today": 500.0,
            # Live quote volume is lots; daily vol_ma20 is shares. Raw division is invalid.
            "volume": 13000, "vol_ma20": 10000000,
            "intraday_volume_ratio": 1.30,
        }
        result = dv.build(classified, market, {"trigger_price": 105.0})
        self.assertIn("量價同步", result["reason_tags"])
        self.assertEqual(result["volume_ratio"], 1.30)


if __name__ == "__main__":
    unittest.main()
