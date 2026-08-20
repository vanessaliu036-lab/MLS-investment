import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import api
import store


class T1ResultSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = self.tmp.name
        store.init_db(self.db)

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def test_t1_streak_uses_verification_date_not_pool_date(self):
        """Pool day is day 4; a positive T+1 must be shown as day 5."""
        dates = [dt.date(2026, 8, 16) + dt.timedelta(days=i) for i in range(5)]
        with sqlite3.connect(self.db) as conn:
            for d in dates:
                conn.execute(
                    "INSERT INTO inst_flow "
                    "(code,data_date,total_net,source,fetched_at) VALUES (?,?,?,?,?)",
                    ("1815", d.isoformat(), 100, "test", "2026-08-20T18:00:00"),
                )
            conn.commit()

        rows = [{"code": "1815"}]
        api._attach_t1_institution(rows, dt.date(2026, 8, 19), self.db)

        self.assertEqual(rows[0]["t1_verify_date"], "2026-08-20")
        self.assertEqual(rows[0]["t1_inst_streak"], 5)
        self.assertEqual(rows[0]["t1_chip_status"], "positive")

    def test_future_card_labels_close_date_as_basis_for_apply_date(self):
        html = (BASE.parent / "intraday_decision_dataflow.html").read_text(encoding="utf-8")
        self.assertIn("收盤基準", html)
        self.assertIn("收盤基準｜供 ${esc2(date)} 使用", html)


if __name__ == "__main__":
    unittest.main()
