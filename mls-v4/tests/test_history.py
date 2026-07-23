import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from history import classify_eod, classify_trend


class HistoryClassificationTest(unittest.TestCase):
    def test_ready_above_ma20_is_actionable(self):
        self.assertEqual(classify_eod({"score": 84, "grade": "Ready", "above_ma20": 1})["group"], "可操作")

    def test_low_score_is_excluded(self):
        self.assertEqual(classify_eod({"score": 45, "grade": "Watch", "above_ma20": 1})["group"], "排除")

    def test_group_rising_is_detected(self):
        rows = [{"group": "觀察"}, {"group": "可操作"}]
        self.assertEqual(classify_trend(rows), "分類爬升")


if __name__ == "__main__":
    unittest.main()
