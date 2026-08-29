import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = os.path.dirname(__file__)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import chips


class OfficialPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cache_file = chips.CACHE_FILE
        chips.CACHE_FILE = os.path.join(self.tmp.name, "chips_cache.json")
        chips._cache = {"date": "", "stocks": {}}

    def tearDown(self):
        chips.CACHE_FILE = self.old_cache_file
        self.tmp.cleanup()

    @staticmethod
    def finmind_stale(dataset, data_id, start_date):
        if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
            return [
                {"date": "2026-08-27", "stock_id": "2303", "name": "Foreign_Investor", "buy": 10_000_000, "sell": 41_746_000},
                {"date": "2026-08-27", "stock_id": "2303", "name": "Investment_Trust", "buy": 1_000_000, "sell": 1_000_000},
            ]
        if dataset == "TaiwanStockHoldingSharesPer":
            return []
        return []

    def _fake_official_module(self):
        mod = types.ModuleType("official_source")
        mod.latest_stock_institutional = lambda code: {
            "code": code,
            "date": "20260828",
            "foreign_lots": 88989,
            "trust_lots": -436,
            "dealer_lots": 2724,
            "total_lots": 91277,
            "source": "TWSE T86",
            "note": None,
        }
        return mod

    def test_detail_prefers_latest_official_t86_over_stale_finmind(self):
        fake = self._fake_official_module()
        with patch.object(chips, "_finmind", side_effect=self.finmind_stale), patch.dict(sys.modules, {"official_source": fake}):
            got = chips.get_chips_detail("2303")
        self.assertEqual(got["foreign_net_d"], 88989)
        self.assertEqual(got["trust_net_d"], -436)
        self.assertEqual(got["dealer_net_d"], 2724)
        self.assertEqual(got["institutional_data_date"], "2026-08-28")
        self.assertEqual(got["institutional_source_type"], "official")

    def test_summary_appends_newer_official_day_before_streak_and_20d_totals(self):
        fake = self._fake_official_module()
        with patch.object(chips, "_finmind", side_effect=self.finmind_stale), patch.dict(sys.modules, {"official_source": fake}):
            got = chips.get_chips("2303")
        # 8/27: -31,746; 8/28 official: +88,989 foreign and -436 trust
        self.assertEqual(got["inst_net_20d_lots"], 56807)
        self.assertEqual(got["inst_streak"], 1)
        self.assertEqual(got["institutional_data_date"], "2026-08-28")
        self.assertEqual(got["institutional_source_type"], "official")

    def test_existing_same_calendar_day_fallback_cache_does_not_block_new_official_data(self):
        chips._cache = {
            "date": chips._today_key(),
            "stocks": {
                "detail:2303": {
                    "foreign_net_d": -31746,
                    "trust_net_d": 0,
                    "dealer_net_d": 0,
                    "foreign_net_20d": -31746,
                    "big400_pct": None,
                    "big400_delta": None,
                    "big1000_pct": None,
                    "big1000_delta": None,
                    "main_force_net": None,
                    "institutional_data_date": "2026-08-27",
                    "institutional_source_type": "finmind_basic",
                }
            },
        }
        chips._save_disk()
        fake = self._fake_official_module()
        with patch.object(chips, "_finmind", side_effect=self.finmind_stale), patch.dict(sys.modules, {"official_source": fake}):
            got = chips.get_chips_detail("2303")
        self.assertEqual(got["foreign_net_d"], 88989)
        self.assertEqual(got["institutional_data_date"], "2026-08-28")
        self.assertEqual(got["institutional_source_type"], "official")


if __name__ == "__main__":
    unittest.main()
