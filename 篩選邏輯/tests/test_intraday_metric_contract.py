import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import intraday_metric_contract as c


class ContractTests(unittest.TestCase):
    def test_volume_and_aflow_are_distinct_even_with_same_unit(self):
        out = c.normalize({
            "total_volume": 10000,
            "buy_volume": 6100,
            "sell_volume": 3900,
            "aflow": 2200,
        })
        self.assertEqual(out["volume_lots"], 10000)
        self.assertEqual(out["aflow_lots"], 2200)
        self.assertEqual(out["aflow_ratio_pct"], 22.0)
        self.assertEqual(out["volume_unit"], "張")
        self.assertEqual(out["aflow_unit"], "張")
        self.assertNotEqual(out["volume_label"], out["aflow_label"])

    def test_aflow_can_derive_from_active_sides_but_never_from_total_volume(self):
        derived = c.normalize({"volume": 8000, "active_buy": 4200, "active_sell": 3100})
        self.assertEqual(derived["aflow_lots"], 1100)
        volume_only = c.normalize({"volume": 8000})
        self.assertIsNone(volume_only["aflow_lots"])

    def test_explicit_aflow_conflict_is_blocked(self):
        out = c.normalize({"total_volume": 10000, "aflow": 1500,
                           "active_buy": 5200, "active_sell": 5000})
        self.assertIsNone(out["aflow_lots"])
        self.assertEqual(out["aflow_status"], "CONFLICT")

    def test_institution_is_always_prior_day_context(self):
        out = c.normalize({
            "institution_label": "法人連買 3 日",
            "chip_data_date": "2026-09-02",
        })
        self.assertEqual(out["institution_asof"], "2026-09-02")
        self.assertEqual(out["institution_metric_label"], "法人籌碼（截至 2026-09-02）")
        self.assertFalse(out["institution_is_intraday"])

    def test_contract_names_do_not_use_ambiguous_buy_sell_label(self):
        labels = c.FIELD_LABELS.values()
        self.assertNotIn("買賣超", labels)
        self.assertEqual(c.FIELD_LABELS["volume"], "成交量")
        self.assertEqual(c.FIELD_LABELS["aflow"], "主動買賣差（A-flow）")


if __name__ == "__main__":
    unittest.main()
