import datetime as _dt
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import screen_verify as sv
import rule_attribution as ra


TODAY = _dt.date.today().isoformat()


def _row(code, tier=None, verification_status=None, ret_pct=None):
    return {
        "data_date": TODAY, "pool_date": TODAY, "code": code,
        "track": "觀察", "trigger_price": None, "base_close": 100.0,
        "next_high": None, "next_close": None, "ma20": None,
        "triggered": None, "hit": None, "ret_pct": ret_pct,
        "verdict": "觀察(不計)",
        "tier": tier, "chase_risk": None,
        "verification_status": verification_status, "entry_status": None,
        "verified_at": "2026-08-19T00:00:00",
    }


class RuleAttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = self.tmp.name
        sv._ensure_table(self.db)

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def _seed(self, rows):
        cols = list(rows[0].keys())
        ph = ",".join("?" * len(cols))
        with sqlite3.connect(self.db) as c:
            c.executemany(
                f"INSERT INTO pool_outcome ({','.join(cols)}) VALUES ({ph})",
                [tuple(r[k] for k in cols) for r in rows],
            )
            c.commit()

    def test_by_tier_and_by_verification_average_ret_correctly(self):
        rows = [
            _row("A001", tier="⏳ 強勢但不追", ret_pct=8.0),
            _row("A002", tier="⏳ 強勢但不追", ret_pct=4.0),
            _row("A003", tier="🔥 A級啟動", ret_pct=1.0),
            _row("A004", verification_status="UNCONFIRMED", ret_pct=9.0),
            _row("A005", verification_status="UNCONFIRMED", ret_pct=-1.0),
            _row("A006", verification_status="CONFIRMED", ret_pct=2.0),
        ]
        self._seed(rows)
        result = sv.stats(days=3650, db_path=self.db)
        by_tier = {r["tier"]: r for r in result["by_tier"]}
        by_ver = {r["verification_status"]: r for r in result["by_verification"]}

        self.assertEqual(by_tier["⏳ 強勢但不追"]["n"], 2)
        self.assertAlmostEqual(by_tier["⏳ 強勢但不追"]["avg_ret"], 6.0)
        self.assertEqual(by_tier["🔥 A級啟動"]["n"], 1)

        self.assertEqual(by_ver["UNCONFIRMED"]["n"], 2)
        self.assertAlmostEqual(by_ver["UNCONFIRMED"]["avg_ret"], 4.0)
        self.assertEqual(by_ver["CONFIRMED"]["n"], 1)

    def test_attribution_flags_downgrade_rule_with_positive_forward_return_as_suspect(self):
        rows = [_row(f"B{i:03d}", tier="⏳ 強勢但不追", ret_pct=6.0) for i in range(6)]
        self._seed(rows)
        out = ra.attribution(days=3650, db_path=self.db)
        suspect_rules = {s["rule"] for s in out["suspects"]}
        self.assertIn("⏳ 強勢但不追", suspect_rules)

    def test_null_tier_and_null_verification_are_excluded_from_grouping(self):
        rows = [_row("C001", ret_pct=5.0)]  # no tier, no verification_status
        self._seed(rows)
        result = sv.stats(days=3650, db_path=self.db)
        self.assertEqual(result["by_tier"], [])
        self.assertEqual(result["by_verification"], [])


if __name__ == "__main__":
    unittest.main()
