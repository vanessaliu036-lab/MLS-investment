import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(MODULE_DIR))

import engine


class MissingChipFieldsTests(unittest.TestCase):
    def test_ai_advice_treats_missing_optional_chip_fields_as_pending(self):
        leader = {"snap": {"change_rate": 1.0}, "sector_type": "attack"}
        advice, stance = engine._ai_advice(
            leader, ma_bias=None, high_space=None,
            ch={"inst_net_20d_lots": 100, "inst_streak": 1},
        )
        self.assertIsInstance(advice, str)
        self.assertIn(stance, {"bullish", "neutral", "caution"})


if __name__ == "__main__":
    unittest.main()
