import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

HERE = os.path.dirname(__file__)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import official_source

TW_TZ = timezone(timedelta(hours=8))


class LatestOfficialInstitutionalTests(unittest.TestCase):
    def test_walks_back_to_latest_available_trading_day(self):
        calls = []

        def fake_stock(code, date=None):
            day = date.strftime("%Y%m%d")
            calls.append(day)
            if day == "20260828":
                return {
                    "code": code, "date": day, "foreign_lots": 88989,
                    "trust_lots": -436, "dealer_lots": 2724,
                    "total_lots": 91277, "source": "TWSE T86", "note": None,
                }
            return {
                "code": code, "date": day, "foreign_lots": None,
                "trust_lots": None, "dealer_lots": None,
                "total_lots": None, "source": "TWSE T86", "note": "no data",
            }

        now = datetime(2026, 8, 29, 11, 2, tzinfo=TW_TZ)  # Saturday
        with patch.object(official_source, "stock_institutional", side_effect=fake_stock):
            got = official_source.latest_stock_institutional("2303", now=now, lookback_days=5)

        self.assertEqual(got["date"], "20260828")
        self.assertEqual(got["foreign_lots"], 88989)
        self.assertEqual(calls[:2], ["20260829", "20260828"])

    def test_t86_parser_maps_umc_20260828_fields_to_lots(self):
        row = ["2303", "聯電", "119,660,000", "30,671,000", "88,989,000",
               "0", "0", "0", "0", "436,000", "-436,000",
               "2,724,000", "0", "0", "0", "0", "0", "0", "91,277,000"]
        payload = {"stat": "OK", "data": [row]}
        official_source._T86_CACHE = {}
        with patch.object(official_source, "_get_json", return_value=payload):
            got = official_source.stock_institutional("2303", date=datetime(2026, 8, 28, tzinfo=TW_TZ))
        self.assertEqual(got["foreign_lots"], 88989)
        self.assertEqual(got["trust_lots"], -436)
        self.assertEqual(got["dealer_lots"], 2724)
        self.assertEqual(got["total_lots"], 91277)

    def test_t86_payload_is_reused_for_multiple_stocks_same_date(self):
        row1 = ["2303", "聯電", "0", "0", "88,989,000", "0", "0", "0", "0", "0", "-436,000", "2,724,000", "0", "0", "0", "0", "0", "0", "91,277,000"]
        row2 = ["2330", "台積電", "0", "0", "1,000,000", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "1,000,000"]
        payload = {"stat": "OK", "data": [row1, row2]}
        official_source._T86_CACHE = {}
        with patch.object(official_source, "_get_json", return_value=payload) as get_json:
            official_source.stock_institutional("2303", date=datetime(2026, 8, 28, tzinfo=TW_TZ))
            official_source.stock_institutional("2330", date=datetime(2026, 8, 28, tzinfo=TW_TZ))
        self.assertEqual(get_json.call_count, 1)


if __name__ == "__main__":
    unittest.main()
