import datetime as _dt
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import store
import b_verify as bv


D = _dt.date(2026, 8, 18)


def _seed(db_path, discoveries, inst_nets):
    store.init_db(db_path)
    with sqlite3.connect(db_path) as c:
        for code, hits, detail_json in discoveries:
            c.execute(
                "INSERT INTO b_discovery (data_date, code, hits, criteria, detail, scanned_at) "
                "VALUES (?,?,?,?,?,?)",
                (D.isoformat(), code, hits, "", detail_json, "2026-08-18T13:20:00"),
            )
        for code, net in inst_nets.items():
            c.execute(
                "INSERT INTO inst_flow (code, data_date, total_net, source, fetched_at) "
                "VALUES (?,?,?,?,?)",
                (code, D.isoformat(), net, "test", "2026-08-18T13:31:00"),
            )
        c.commit()


class BVerifyTiersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = self.tmp.name

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def test_low_confidence_is_unconfirmed_not_dropped(self):
        """鐵律:confidence < 50 只能標 UNCONFIRMED,不得從結果消失(取代舊 FAIL 淘汰)。"""
        _seed(self.db, [("2330", 0, '{"passed": []}')], {"2330": -3000})
        res = bv.verify(self.db, D)
        self.assertEqual(len(res["verified"]), 1)
        self.assertEqual(res["verified"][0]["verification_status"], bv.VERDICT_UNCONFIRMED)
        self.assertEqual(len(res["unconfirmed"]), 1)
        self.assertNotIn("failed", res)
        self.assertNotIn("passed", res)

    def test_three_confidence_tiers(self):
        discoveries = [
            ("A001", 2, '{"passed": ["持續性", "下殺承接", "相對族群強度", "量增價穩"]}'),  # 80 行為分
            ("A002", 1, '{"passed": ["持續性", "量增價穩"]}'),  # 35 行為分
            ("A003", 0, '{"passed": []}'),          # 0 行為分
        ]
        inst = {"A001": 600, "A002": 500, "A003": -600}
        _seed(self.db, discoveries, inst)
        res = bv.verify(self.db, D)
        by_code = {v["code"]: v for v in res["verified"]}
        self.assertEqual(by_code["A001"]["verification_status"], bv.VERDICT_CONFIRMED)
        self.assertEqual(by_code["A002"]["verification_status"], bv.VERDICT_PARTIAL)
        self.assertEqual(by_code["A003"]["verification_status"], bv.VERDICT_UNCONFIRMED)
        self.assertEqual(len(res["verified"]), 3)

    def test_missing_inst_data_is_no_data_not_fail(self):
        _seed(self.db, [("9999", 0, '{"passed": []}')], {})
        res = bv.verify(self.db, D)
        self.assertEqual(res["verified"][0]["verification_status"], bv.VERDICT_NO_DATA)
        self.assertEqual(len(res["no_data"]), 1)

    def test_load_verified_returns_all_codes_regardless_of_status(self):
        discoveries = [
            ("A001", 2, '{"passed": ["持續性", "下殺承接", "相對族群強度", "量增價穩"]}'),
            ("A003", 0, '{"passed": []}'),
        ]
        _seed(self.db, discoveries, {"A001": 600, "A003": -600})
        bv.verify(self.db, D)
        codes = set(bv.load_verified(D, self.db))
        self.assertEqual(codes, {"A001", "A003"})


if __name__ == "__main__":
    unittest.main()
